// dejavu-rescue — recover records from torn / truncated Weaviate LSM
// "objects" bucket segments. Pure-stdlib Go port of the v0.8.2 POC
// (see /tmp/dejavu-rescue-poc/main.go). No Weaviate imports.
//
// The format we walk is the Weaviate v1.27.x objects bucket on-disk
// segment ("strategy=0", replace bucket):
//
//   header (16 bytes, little-endian):
//     uint16 level
//     uint16 version
//     uint16 secondaryIndices  (count)
//     uint16 strategy           (0 = replace bucket)
//     uint64 indexStart         (offset where data ends, index starts)
//
//   data region (from offset 16 .. indexStart):
//     repeat:
//       uint8  tombstone (0|1)
//       uint64 valueLen
//       value[valueLen]                       — storobj-encoded object
//       uint32 primaryKeyLen
//       key[primaryKeyLen]
//       for each secondary index:
//         uint32 secondaryKeyLen
//         secondaryKey[secondaryKeyLen]
//
//   storobj payload (marshaller version 1):
//     uint8  marshallerVersion  (1)
//     uint64 docID
//     uint8  kind
//     [16]byte uuid
//     uint64 createTime
//     uint64 updateTime
//     uint16 vectorLen          (float32 count)
//     float32[vectorLen] vector
//     uint16 classNameLen
//     classNameBytes[classNameLen]
//     uint32 schemaJSONLen
//     schemaJSON[schemaJSONLen]
//     (trailing fields: vectorIndexID, additionalProperties — best-effort
//      parsed but optional)
//
// On torn writes the indexStart in the header can lie (the trailing
// region is all-zeros from a sparse-file extension that crashed mid-
// flush). We additionally clamp the walk to the last non-zero byte,
// and stop on the first record whose plausibility check fails:
//   - storobj decode OK
//   - className non-empty
//   - vectorLen > 0
//   - properties JSON parses to an object
package main

import (
	"bufio"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io/fs"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

const headerSize = 16

// Sanity bounds — if a record's declared length exceeds these we treat
// it as torn-write garbage and stop. ClaudeDejavuTurn values cap around
// ~8 KB; we leave a generous 10 MB ceiling so user-defined classes with
// large blobs still work.
const (
	maxValueLen     = 10 * 1024 * 1024
	maxKeyLen       = 1024
	maxSecondaryLen = 1024
)

// Header is the 16-byte LSM segment header.
type Header struct {
	Level            uint16 `json:"level"`
	Version          uint16 `json:"version"`
	SecondaryIndices uint16 `json:"secondary_indices"`
	Strategy         uint16 `json:"strategy"`
	IndexStart       uint64 `json:"index_start"`
}

// Record is the recovered, decoded record we emit as one JSONL line.
type Record struct {
	UUID          string                 `json:"uuid"`
	DocID         uint64                 `json:"doc_id"`
	ClassName     string                 `json:"class_name"`
	VectorLen     uint16                 `json:"vector_len"`
	Vector        []float32              `json:"vector,omitempty"`
	Properties    map[string]interface{} `json:"properties,omitempty"`
	Tombstone     bool                   `json:"tombstone"`
	PrimaryKeyHex string                 `json:"primary_key_hex,omitempty"`
	SourceSegment string                 `json:"source_segment"`
	ByteOffset    uint64                 `json:"byte_offset"`
	Plausible     bool                   `json:"plausible"`
	DecodeNote    string                 `json:"decode_note,omitempty"`
}

// SegmentReport is a per-segment summary emitted by `inspect`.
type SegmentReport struct {
	FilePath          string `json:"file_path"`
	FileSize          int64  `json:"file_size"`
	Header            Header `json:"header"`
	LastNonZeroOffset int64  `json:"last_non_zero_offset"`
	EffectiveWalkEnd  uint64 `json:"effective_walk_end"`
	RecordsRead       int    `json:"records_read"`
	PlausibleRecords  int    `json:"plausible_records"`
	Tombstones        int    `json:"tombstones"`
	TruncationOffset  uint64 `json:"truncation_offset"`
	TruncationReason  string `json:"truncation_reason"`
}

// ─── byte-level helpers ─────────────────────────────────────────────────

func safeSlice(data []byte, off, n uint64) ([]byte, bool) {
	end := off + n
	if end < off || end > uint64(len(data)) {
		return nil, false
	}
	return data[off:end], true
}

func lastNonZero(data []byte) int64 {
	for i := len(data) - 1; i >= 0; i-- {
		if data[i] != 0 {
			return int64(i)
		}
	}
	return -1
}

func formatUUID(b [16]byte) string {
	return fmt.Sprintf("%s-%s-%s-%s-%s",
		hex.EncodeToString(b[0:4]),
		hex.EncodeToString(b[4:6]),
		hex.EncodeToString(b[6:8]),
		hex.EncodeToString(b[8:10]),
		hex.EncodeToString(b[10:16]),
	)
}

// ─── storobj decoder ────────────────────────────────────────────────────

// decodeStorobj decodes a value payload into a Record. On any short read
// it sets DecodeNote and leaves Plausible=false; the caller decides
// whether to keep walking or stop.
func decodeStorobj(value []byte, rec *Record) {
	if len(value) < 1 {
		rec.DecodeNote = "empty value"
		return
	}
	if value[0] != 1 {
		rec.DecodeNote = fmt.Sprintf("unsupported marshaller version %d", value[0])
		return
	}
	if len(value) < 44 {
		rec.DecodeNote = "value too short for storobj header"
		return
	}

	pos := 1
	rec.DocID = binary.LittleEndian.Uint64(value[pos : pos+8])
	pos += 8
	pos++ // kind
	var uuidBytes [16]byte
	copy(uuidBytes[:], value[pos:pos+16])
	rec.UUID = formatUUID(uuidBytes)
	pos += 16
	pos += 8 // createTime
	pos += 8 // updateTime

	if pos+2 > len(value) {
		rec.DecodeNote = "truncated before vectorLen"
		return
	}
	vecLen := binary.LittleEndian.Uint16(value[pos : pos+2])
	pos += 2
	rec.VectorLen = vecLen
	vecBytes := int(vecLen) * 4
	if pos+vecBytes > len(value) {
		rec.DecodeNote = "truncated mid-vector"
		return
	}
	if vecLen > 0 {
		rec.Vector = make([]float32, int(vecLen))
		for i := 0; i < int(vecLen); i++ {
			bits := binary.LittleEndian.Uint32(value[pos+i*4 : pos+i*4+4])
			rec.Vector[i] = math.Float32frombits(bits)
		}
	}
	pos += vecBytes

	if pos+2 > len(value) {
		rec.DecodeNote = "truncated before className length"
		return
	}
	classLen := binary.LittleEndian.Uint16(value[pos : pos+2])
	pos += 2
	if pos+int(classLen) > len(value) {
		rec.DecodeNote = "truncated mid-className"
		return
	}
	rec.ClassName = string(value[pos : pos+int(classLen)])
	pos += int(classLen)

	if pos+4 > len(value) {
		rec.DecodeNote = "truncated before schema length"
		return
	}
	schemaLen := binary.LittleEndian.Uint32(value[pos : pos+4])
	pos += 4
	if pos+int(schemaLen) > len(value) {
		rec.DecodeNote = "truncated mid-schema"
		return
	}
	schemaBytes := value[pos : pos+int(schemaLen)]

	var props map[string]interface{}
	if err := json.Unmarshal(schemaBytes, &props); err != nil {
		rec.DecodeNote = "schema not JSON: " + err.Error()
		return
	}
	rec.Properties = props
}

// isPlausible checks a record's plausibility independent of class name.
// We generalized the POC: any class with vectorLen > 0, non-empty
// className, and at least one parsed property counts as a real record.
func isPlausible(r *Record) bool {
	if r.DecodeNote != "" {
		return false
	}
	if r.ClassName == "" {
		return false
	}
	if r.VectorLen == 0 {
		return false
	}
	if len(r.Properties) == 0 {
		return false
	}
	return true
}

// ─── segment walker ─────────────────────────────────────────────────────

// walkSegment reads one segment file end-to-end. The records slice
// contains every record that decoded plausibly OR is a clean tombstone;
// records that fail plausibility are still appended (with Plausible=false)
// so the caller can audit them. The report carries the walk statistics.
func walkSegment(path string) ([]Record, SegmentReport, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, SegmentReport{}, fmt.Errorf("read %s: %w", path, err)
	}
	if len(data) < headerSize {
		return nil, SegmentReport{
			FilePath: path, FileSize: int64(len(data)),
			TruncationReason: "file shorter than 16-byte header",
		}, nil
	}

	h := Header{
		Level:            binary.LittleEndian.Uint16(data[0:2]),
		Version:          binary.LittleEndian.Uint16(data[2:4]),
		SecondaryIndices: binary.LittleEndian.Uint16(data[4:6]),
		Strategy:         binary.LittleEndian.Uint16(data[6:8]),
		IndexStart:       binary.LittleEndian.Uint64(data[8:16]),
	}

	rep := SegmentReport{
		FilePath: path, FileSize: int64(len(data)), Header: h,
	}

	// We only support the replace bucket (strategy=0) which is what
	// Weaviate uses for the objects bucket. Other strategies (set / map
	// / roaring set) have different on-disk record framing.
	if h.Strategy != 0 {
		rep.TruncationReason = fmt.Sprintf(
			"unsupported strategy %d (only strategy=0/replace is parseable)",
			h.Strategy)
		return nil, rep, nil
	}
	if h.Version != 0 {
		rep.TruncationReason = fmt.Sprintf(
			"unsupported version %d (only version=0 is parseable)",
			h.Version)
		return nil, rep, nil
	}

	lastNZ := lastNonZero(data)
	rep.LastNonZeroOffset = lastNZ

	// effective walk end = min(IndexStart, lastNonZero+1, file size)
	effectiveEnd := h.IndexStart
	if uint64(lastNZ+1) < effectiveEnd {
		effectiveEnd = uint64(lastNZ + 1)
	}
	if effectiveEnd > uint64(len(data)) {
		effectiveEnd = uint64(len(data))
	}
	rep.EffectiveWalkEnd = effectiveEnd

	var (
		off       uint64 = headerSize
		records   []Record
		stop      bool
		stopWhy   string
		secCount         = int(h.SecondaryIndices)
		nTomb     int
		nPlausi   int
	)

	for off < effectiveEnd && !stop {
		recStart := off

		hdr, ok := safeSlice(data, off, 9)
		if !ok {
			stopWhy = "short read on record header"
			break
		}
		tomb := hdr[0] == 0x1
		valueLen := binary.LittleEndian.Uint64(hdr[1:9])
		off += 9

		if valueLen > maxValueLen {
			stopWhy = fmt.Sprintf("implausible valueLen %d at offset %d",
				valueLen, recStart)
			off = recStart
			break
		}
		if valueLen > uint64(len(data))-off {
			stopWhy = fmt.Sprintf("valueLen %d overruns file (off=%d, size=%d)",
				valueLen, off, len(data))
			off = recStart
			break
		}
		value, ok := safeSlice(data, off, valueLen)
		if !ok {
			stopWhy = "value slice out of range"
			off = recStart
			break
		}
		off += valueLen

		klBytes, ok := safeSlice(data, off, 4)
		if !ok {
			stopWhy = "short read on primaryKeyLen"
			off = recStart
			break
		}
		keyLen := binary.LittleEndian.Uint32(klBytes)
		off += 4
		if keyLen > maxKeyLen {
			stopWhy = fmt.Sprintf("implausible primaryKeyLen %d", keyLen)
			off = recStart
			break
		}
		key, ok := safeSlice(data, off, uint64(keyLen))
		if !ok {
			stopWhy = "primaryKey slice out of range"
			off = recStart
			break
		}
		off += uint64(keyLen)

		secBroke := false
		for j := 0; j < secCount; j++ {
			skl, ok := safeSlice(data, off, 4)
			if !ok {
				stopWhy = fmt.Sprintf("short read on secondary key %d length", j)
				secBroke = true
				break
			}
			secLen := binary.LittleEndian.Uint32(skl)
			off += 4
			if secLen > maxSecondaryLen {
				stopWhy = fmt.Sprintf("implausible secondaryKeyLen %d at index %d",
					secLen, j)
				secBroke = true
				break
			}
			if secLen == 0 {
				continue
			}
			if _, ok := safeSlice(data, off, uint64(secLen)); !ok {
				stopWhy = fmt.Sprintf("secondary key %d slice out of range", j)
				secBroke = true
				break
			}
			off += uint64(secLen)
		}
		if secBroke {
			off = recStart
			break
		}

		rec := Record{
			Tombstone:     tomb,
			PrimaryKeyHex: hex.EncodeToString(key),
			SourceSegment: filepath.Base(path),
			ByteOffset:    recStart,
		}
		// Tombstone records have an empty value (valueLen=0). Skip the
		// storobj decode in that case; we still surface the record so
		// callers know UUIDs marked for deletion.
		if !tomb && valueLen > 0 {
			decodeStorobj(value, &rec)
		}
		rec.Plausible = isPlausible(&rec) || (tomb && len(rec.PrimaryKeyHex) > 0)

		if tomb {
			nTomb++
		}
		if rec.Plausible {
			nPlausi++
		}

		// Tear-stop: if a record is implausible AND we're past the
		// "all zero tail" boundary, stop here. The POC found exactly
		// one trailing implausible record per torn segment.
		if !rec.Plausible && !tomb {
			stopWhy = fmt.Sprintf("implausibility at offset %d (decode_note=%q)",
				recStart, rec.DecodeNote)
			// Surface the failed record one more time so the operator
			// can see it in --json mode, then stop.
			records = append(records, rec)
			break
		}

		records = append(records, rec)
	}

	if stopWhy == "" {
		stopWhy = fmt.Sprintf("walked clean to effectiveEnd=%d", off)
	}
	rep.RecordsRead = len(records)
	rep.PlausibleRecords = nPlausi
	rep.Tombstones = nTomb
	rep.TruncationOffset = off
	rep.TruncationReason = stopWhy

	return records, rep, nil
}

// ─── subcommands ────────────────────────────────────────────────────────

type inspectFlags struct {
	jsonOut bool
}

func runInspect(args []string) int {
	fs_ := flag.NewFlagSet("inspect", flag.ContinueOnError)
	var fl inspectFlags
	fs_.BoolVar(&fl.jsonOut, "json", true, "emit JSON report on stdout (default true)")
	if err := fs_.Parse(args); err != nil {
		fmt.Fprintln(os.Stderr, "inspect: parse args:", err)
		return 2
	}
	rest := fs_.Args()
	if len(rest) != 1 {
		fmt.Fprintln(os.Stderr,
			"usage: dejavu-rescue inspect [--json] <segment.db>")
		return 2
	}
	path := rest[0]
	_, rep, err := walkSegment(path)
	if err != nil {
		fmt.Fprintln(os.Stderr, "inspect:", err)
		return 1
	}
	if fl.jsonOut {
		out, _ := json.MarshalIndent(rep, "", "  ")
		fmt.Println(string(out))
	} else {
		fmt.Printf("file=%s size=%d records=%d plausible=%d tombstones=%d "+
			"truncation_offset=%d reason=%s\n",
			rep.FilePath, rep.FileSize, rep.RecordsRead, rep.PlausibleRecords,
			rep.Tombstones, rep.TruncationOffset, rep.TruncationReason)
	}
	return 0
}

type scanShardFlags struct {
	out      string
	include  string
	jsonOnly bool
}

// walkObjectsDir locates an "objects" directory under shardDir. Accepts
// either the shard dir itself (lsm/objects/*.db inside) or a path that
// directly contains *.db segments.
func walkObjectsDir(shardDir string) (string, []string, error) {
	// Most common shape: <shardDir>/lsm/objects/*.db
	candidate := filepath.Join(shardDir, "lsm", "objects")
	if st, err := os.Stat(candidate); err == nil && st.IsDir() {
		segs, err := findSegments(candidate)
		return candidate, segs, err
	}
	// Fallback: <shardDir>/objects/*.db
	candidate = filepath.Join(shardDir, "objects")
	if st, err := os.Stat(candidate); err == nil && st.IsDir() {
		segs, err := findSegments(candidate)
		return candidate, segs, err
	}
	// Fallback: shardDir itself contains *.db
	segs, err := findSegments(shardDir)
	if err == nil && len(segs) > 0 {
		return shardDir, segs, nil
	}
	return "", nil, fmt.Errorf(
		"no objects bucket found under %s (looked for lsm/objects, "+
			"objects, and direct *.db)", shardDir)
}

func findSegments(dir string) ([]string, error) {
	var out []string
	err := filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			// Don't recurse — the segments live flat in the objects dir.
			if path == dir {
				return nil
			}
			return fs.SkipDir
		}
		name := d.Name()
		if !strings.HasPrefix(name, "segment-") {
			return nil
		}
		if !strings.HasSuffix(name, ".db") {
			return nil
		}
		out = append(out, path)
		return nil
	})
	if err != nil && !errors.Is(err, fs.SkipDir) {
		return nil, err
	}
	sort.Strings(out)
	return out, nil
}

func runScanShard(args []string) int {
	fs_ := flag.NewFlagSet("scan-shard", flag.ContinueOnError)
	var fl scanShardFlags
	fs_.StringVar(&fl.out, "out", "",
		"path to JSONL output (default: stdout)")
	fs_.StringVar(&fl.include, "include-quarantined", "",
		"additional segment file to include (e.g. a quarantined .db)")
	fs_.BoolVar(&fl.jsonOnly, "json", false,
		"summary report to stderr as JSON instead of human text")
	if err := fs_.Parse(args); err != nil {
		fmt.Fprintln(os.Stderr, "scan-shard: parse args:", err)
		return 2
	}
	rest := fs_.Args()
	if len(rest) != 1 {
		fmt.Fprintln(os.Stderr,
			"usage: dejavu-rescue scan-shard [--out <p>] "+
				"[--include-quarantined <p>] <shard-dir>")
		return 2
	}
	shardDir := rest[0]

	objectsDir, segments, err := walkObjectsDir(shardDir)
	if err != nil {
		fmt.Fprintln(os.Stderr, "scan-shard:", err)
		return 1
	}
	if fl.include != "" {
		segments = append(segments, fl.include)
	}

	// Output sink.
	var sink *os.File
	if fl.out == "" {
		sink = os.Stdout
	} else {
		f, err := os.Create(fl.out)
		if err != nil {
			fmt.Fprintln(os.Stderr, "scan-shard: open out:", err)
			return 1
		}
		defer f.Close()
		sink = f
	}
	w := bufio.NewWriter(sink)
	defer w.Flush()
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)

	totals := struct {
		Segments  int             `json:"segments"`
		ObjectsDir string         `json:"objects_dir"`
		Records   int             `json:"records"`
		Plausible int             `json:"plausible"`
		Tombstone int             `json:"tombstones"`
		Reports   []SegmentReport `json:"reports"`
	}{ObjectsDir: objectsDir}

	for _, seg := range segments {
		recs, rep, err := walkSegment(seg)
		if err != nil {
			fmt.Fprintf(os.Stderr,
				"scan-shard: segment %s failed: %v\n", seg, err)
			continue
		}
		for i := range recs {
			r := recs[i]
			// Only emit plausible records OR tombstones — implausible
			// trailing garbage is interesting in the summary but useless
			// to the orchestrator.
			if !r.Plausible && !r.Tombstone {
				continue
			}
			if err := enc.Encode(&r); err != nil {
				fmt.Fprintln(os.Stderr, "scan-shard: encode:", err)
				return 1
			}
		}
		totals.Segments++
		totals.Records += rep.RecordsRead
		totals.Plausible += rep.PlausibleRecords
		totals.Tombstone += rep.Tombstones
		totals.Reports = append(totals.Reports, rep)
	}

	// Summary line on stderr — orchestrator parses it to learn what
	// happened during the scan.
	summary, _ := json.Marshal(&totals)
	fmt.Fprintln(os.Stderr, string(summary))
	return 0
}

// ─── main ───────────────────────────────────────────────────────────────

const usage = `dejavu-rescue — recover Weaviate LSM segment records

Usage:
  dejavu-rescue inspect [--json] <segment.db>
  dejavu-rescue scan-shard [--out <jsonl>] [--include-quarantined <path>] <shard-dir>

Subcommands:
  inspect       Parse one segment and print header + record statistics.
  scan-shard    Walk every segment under <shard-dir>/lsm/objects/ and
                emit one JSONL record per recovered object.
`

func main() {
	if len(os.Args) < 2 {
		fmt.Fprint(os.Stderr, usage)
		os.Exit(2)
	}
	switch os.Args[1] {
	case "inspect":
		os.Exit(runInspect(os.Args[2:]))
	case "scan-shard":
		os.Exit(runScanShard(os.Args[2:]))
	case "-h", "--help", "help":
		fmt.Print(usage)
		os.Exit(0)
	case "version", "--version":
		fmt.Println("dejavu-rescue 0.8.2")
		os.Exit(0)
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand %q\n%s",
			os.Args[1], usage)
		os.Exit(2)
	}
}
