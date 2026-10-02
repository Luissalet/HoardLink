# Docs: search, chunking, sniffing, readers, vectors, images

What the family copied between its "documents and search" apps, in one place. Python in `hoard_link/docs/` (stdlib only at import;
Pillow, numpy, pypdfium2 and pypdf are imported lazily and a missing one raises `Unavailable` via `hoard_link.errors.missing_dependency`),
Node twin in `js/hoard-commons/docs.js` (ESM, no dependencies, Node 18+). Rules: `docs/COMMONS.md`.
Text helpers (`fold`, `slugify`, `safe_filename`, `sha256_file`) come from `hoard_link/text.py` and are not duplicated here.

Tests: `tests/commons/test_docs_{textsearch,chunking,pageranges,vecmath,sniff,archives,readers,textclean,imaging,twin}.py`
(329 tests). Golden vectors in `tests/vectors/docs_{fts,ranges,chunks,sniff,cite,vec}.json` (154 + 89 + 22 + 81 + 28 + 37 cases) are generated
from Python and run by both Python and Node in `test_docs_twin.py`.

| Module | What it does | Node twin (`docs.js`) |
|---|---|---|
| `textsearch` | injection-proof FTS5 queries, query ladder, smoothed-IDF BM25 re-scoring, stopwords, stemming, highlights | `ftsQuery`, `ftsLadder`, `queryTerms`, `tokens`, `stem`, `contentWords`, `highlight`, `snippet`, `STOPWORDS`, `isStopword` |
| `chunking` | Borges chunker (paragraph > line > sentence > clause > word, overlap, tail absorption), page and section aware, Markdown with breadcrumbs | `chunkText`, `chunkUnits`, `mergeSmallUnits`, `CHUNK_VERSION` |
| `sniff` | what a file really is, from magic bytes and zip member names, never from the name alone | `sniff`, `mimeFor`, `extOf`, `zipMemberNames`, `MIME` |
| `pageranges` | `"1-3, 5, 8-"`, `"last"`, `"odd"` page lists with bilingual errors | `parseRanges`, `parseGroups`, `describeRanges`, `PageRangeError` |
| `vecmath` | float32 BLOB vectors, cosine, top-k, reciprocal-rank and min-max fusion | `packVec`, `unpackVec`, `normalize`, `cosine`, `dot`, `topk`, `rrf`, `minmaxFuse` |
| `citations` | one citation string format for docs, pages, chapters, chats, links, mail, code | `cite`, `CITE_KINDS` |
| `textclean` | tidy extracted text, drop repeated headers/footers, decode bytes, split long text | - |
| `readers_lite` | stdlib readers for docx, odt/ods/odp, rtf, html, pptx, xlsx, epub, eml, plus pdf via pypdfium2/pypdf; `read_any` | - |
| `imaging` | orient / thumbnail / EXIF / GPS strip / perceptual hashes / shrink to a byte limit | - |
| `archives` | safe file names, collision-free writes, zip-slip guard, zip-bomb guard | - |

Options in Python are keyword arguments (`size=900, overlap=150`); in Node they are one trailing camelCase object
(`{size: 900, overlap: 150}`). Error messages are identical in both. Chunk offsets in Node count UTF-16 code units, so text with
characters outside the Basic Multilingual Plane (emoji) can cut a few characters differently from Python.

## `hoard_link.docs.textsearch`

```python
fts_query(q, *, mode="and"|"prefix"|"or", max_terms=12) -> str     # "" when nothing searchable
fts_ladder(q, *, max_terms=12) -> list[str]                        # exact AND, then prefix AND, then prefix OR (deduplicated)
query_terms(q, *, max_terms=12) -> list                            # terms for bm25_rescore / highlight (prefix terms end in "*")
ensure_fts(conn, table, columns, *, tokenize="unicode61 remove_diacritics 2", content=None) -> bool
fts_available(conn) -> bool
bm25_rescore(rows, query_terms, *, k1=1.2, b=0.75, weights=None, idf_floor=0.1, idfs=None, length_column=None) -> [(row, score)]
highlight(text, terms, *, window=160, tag="mark", escape=True) -> str
snippet(text, terms=(), *, window=160) -> str
tokens(text, *, stop=None) -> list[str] ; stem(word) ; content_words(text) ; is_stopword(word)
STOPWORDS, STOPWORDS_FOLDED, PREFIX_MIN_CHARS = 3
```

* **Cannot inject.** Every term is double-quoted and `"` is doubled, so `AND`, `OR`, `NOT`, `NEAR`, `-`, `*`, `:`, `^`, `(` in user text are
  plain words. Tested against a real FTS5 table with hostile strings and 400 random queries.
* **Ladder.** Run the rungs in order and stop at the first that returns rows: `"hojas" AND "verdes"`, then `"hoja"* AND "verd"*`, then the
  same with `OR`. Prefix terms use `stem` (plural and verb endings dropped) and only apply to words of 3+ characters.
* **Stopwords** (Spanish and English) are dropped from queries unless nothing else is left; matching is done on the folded form, so
  `"más"`, `"mas"` and `"MÁS"` are the same word (Borges' accented entries never matched).
* **`bm25_rescore`** replaces FTS5's own `bm25()`, which gives 0 to a term present in every row. It uses smoothed IDF (never negative, floor
  `idf_floor`), per-column `weights`, and accepts dict rows, strings or objects. Terms ending in `*` match word starts.
* **`highlight`** works on the original text: the fold is one character per character, so offsets always line up. Marks word starts only
  (`cat` marks `category` and `cat`, not `scatter`), escapes HTML unless `escape=False`, and cuts a `window` with `…`.
* `ensure_fts` validates identifiers, returns `False` when SQLite has no FTS5 (callers then fall back to `LIKE`) and supports external content.

## `hoard_link.docs.chunking`

```python
@dataclass Unit(kind="section", number=1, title="", text="", line_start=None)    # a page, section, chapter or slide
@dataclass Chunk(unit_index, ordinal, page, section, line, char_start, char_end, text)
chunk_text(text, *, size=900, overlap=150, min_tail=200) -> list[Chunk]
chunk_units(units, *, size=900, overlap=150, min_unit=200, min_chunk=120, min_tail=200) -> list[Chunk]
merge_small_units(units, minimum=200) -> list[Unit]
chunk_markdown(text, *, size=1200, breadcrumbs=True, frontmatter=True, default_title="") -> list[Chunk]
markdown_title(text, default="", *, frontmatter=True) ; parse_frontmatter(text) -> (dict, end_offset)
CHUNK_VERSION = 3
```

* `chunk_units` takes `Unit` objects, dicts or any object with the same attributes, so `readers_lite` units go straight in.
* Cut priority: paragraph break, line break, sentence end, clause (`, ; :`), word, hard cut. A chunk is never empty or padded; `char_start` /
  `char_end` index the original text and `text[char_start:char_end] == chunk.text` always holds.
* **Pages are never merged** (a citation to "p. 7" must stay true); short non-page units (sections, slides) are merged into a neighbour.
  A final piece shorter than `min_tail` joins the previous chunk; tiny chunks inside a unit are glued to a neighbour.
* `overlap` is clamped to `size // 2` so the window always advances.
* `chunk_markdown` keeps a heading stack: the chunk `section` reads `Guide § Install § Windows`; fenced code is never read as headings;
  YAML front matter supplies the title and is not indexed; paragraphs are packed up to `size`, huge ones cut at sentences.
* Bump `CHUNK_VERSION` whenever the chunker output changes; indexes store it to know when to rebuild.

## `hoard_link.docs.sniff`

```python
sniff(name, data) -> Sniffed(kind, mime, ext)          # never raises; data = the whole file (a zip needs its end), or its first KB for non-zips
mime_for(ext) -> str ; ext_of(name) -> str ; zip_member_names(data) -> list[str] | None ; MIME
```

Kinds: `pdf image docx xlsx pptx odt ods odp epub rtf ole audio video html json csv eml text zip archive sqlite parquet unknown`.
Magic bytes win over the file name (a `.pdf` that is a PNG is an image); Office and OpenDocument files are told apart by the member names in the
zip central directory (with a local-header scan for truncated downloads); text must decode and be free of control bytes; MP3 frame sync
is accepted only with a matching name; `.eml` needs mail headers to count as mail.

## `hoard_link.docs.pageranges`

```python
parse_ranges(text, total, *, default_all=False, unique=True, allow_reversed=False, lang="es") -> list[int]     # 1-based, in order
parse_groups(text, total, *, lang="es") -> list[list[int]]                                                     # "1-3; 5" -> [[1,2,3],[5]]
describe(pages) -> str                                                                                         # [1,2,3,5] -> "1-3,5"   (Node: describeRanges)
class PageRangeError(ValueError)  # .message_es, .message_en, .lang; str() is the message in `lang`
```

Grammar: `3`, `2-5`, `7-` (to the end), `-1` (the last page, `-2` the one before), `last` / `última`, `3-last`, `odd` / `impares`,
`even` / `pares`, `all` / `todas`, plus `pág. 3`, `1 a 3`, `2 y 5`; separated by commas or semicolons. Out of range, reversed (unless
`allow_reversed`), unknown words and empty input are errors with an example list, in Spanish and English; nothing is silently dropped.

## `hoard_link.docs.vecmath`

```python
pack_vec(values) -> bytes ; unpack_vec(blob, dim=None) -> list[float]          # float32, little endian
normalize(v) ; dot(a, b) ; cosine(a, b)
topk(matrix, query, k, min_score=0.0, *, normalized=False) -> [(index, score)]   # numpy argpartition when numpy is installed, heapq otherwise
rrf(rankings, weights=None, k=60) -> [(id, score)]                               # reciprocal rank fusion of several ranked id lists
minmax_fuse(scored, weights=None) -> [(id, score)]                               # min-max normalise each list, weighted sum
```

The BLOB layout is the one Borges (numpy `float32`) and Hypatia (`array('f')`) already store, so existing databases keep working.
Node: `packVec` returns a `Buffer`; `unpackVec(blob, dim)` or `unpackVec(blob, {dim})`.

## `hoard_link.docs.citations`

```python
cite(kind, title, *, page=None, section=None, line=None, date=None, turn=None, site=None, lang="es") -> str
KINDS = doc, page, book, chat, link, mail, code
```

Spanish: `«Contrato», p. 12` (doc/page), `informe.md § Resultados`, `informe.md, l. 40`, `«El Aleph», cap. 3` (book),
`[chat «Viaje» · 2026-09-30 · turno 4]`, `[enlace «Receta» · example.org]`, `[correo «Factura» · banco@example.org · 2026-10-01]`,
`src/app.py § Install:42` (code). English (`lang="en"`): curly quotes, `ch.`, `line`, `turn`, `[link ...]`, `[mail ...]`. Always the same
punctuation, so the UI and the model prompts quote sources identically.

## `hoard_link.docs.textclean`

```python
clean_text(text, *, dehyphenate=True, unstack=True, max_chars=None) -> str
unstack_words(text) ; strip_repeated_lines(pages, threshold=0.5) -> list[str] ; useful_chars(text) -> int
decode_text(data, *, reject_binary=False) -> str ; split_pages(text, size=6000) -> list[str]
```

`clean_text` removes NUL, soft hyphens, zero-width characters and BOMs, normalises spaces and blank lines, re-joins `informa-\nción` (only
before a lowercase letter) and one-word-per-line PDF output. `strip_repeated_lines` drops running headers and footers (digits ignored, so
`Page 3` equals `Page 4`; needs 3+ pages). `decode_text`: BOM, then UTF-8, then Windows-1252, then Latin-1; `reject_binary` refuses a NUL in the first 4 KB.

## `hoard_link.docs.readers_lite`

```python
read_any(name, data, *, max_pages=400, max_chars=5_000_000) -> {kind, title, text, units, needs_ocr, notes, mime, error}   # never raises
read_docx / read_odt / read_pptx / read_xlsx / read_epub / read_pdf(data, ...) -> list[unit]       # unit = {kind, number, title, text}
read_rtf(data) -> str ; read_html(data) -> str ; html_to_text(html) -> str ; read_eml(data) -> str
check_zip(zf, max_unzipped=300_000_000, max_ratio=200, *, max_entries=20_000)    # raises ZipBombError
```

* **Units** feed `chunking.chunk_units` directly: docx and odt split by heading (`section`), pdf by `page`, pptx by `slide`, xlsx and ods by `sheet`,
  epub by `chapter` in spine order. Lists come out as `- item`, tables as `a | b`; tracked deletions, footnotes bodies and `mc:Fallback`
  duplicates are skipped.
* **PDF**: pypdfium2 first, pypdf as fallback, neither: `read_any` returns `needs_ocr` with a note. A page with fewer than 40 letters or digits is
  marked `needs_ocr`; the family's OCR service (`fam_docs`) handles those, the light reader does not. Repeated headers and footers are stripped.
* **`read_html`** uses `hoard_link.web.htmltext.readable` when that module exists (looked up at call time, so there is no import cycle) and the
  built-in parser otherwise; either way one line per block.
* **Zip safety**: total uncompressed size, per-member ratio (members of 10 MB or more) and entry count are checked before anything is read.
* Heavy extractors (OCR, tables in PDFs, scanned documents, legacy `.doc`/`.xls`) are not here; `read_any` reports them as unsupported with a note.

## `hoard_link.docs.imaging`

```python
open_oriented(src, *, max_pixels=None, draft=None) -> PIL.Image          # EXIF-rotated, RGB / RGBA / L
flatten_rgb(img, bg=(255, 255, 255)) ; register_heif() -> bool
thumbnail(src, *, size=512, fmt="webp", quality=80) -> bytes            # never upscales
read_exif(src) -> dict          # width height orientation date lat lon camera make model lens iso f_number exposure focal_length
strip_exif(src, *, gps_only=False, keep_orientation=True, strict=True) -> bytes
phash(img) -> int (64-bit, equal to imagehash.phash) ; dhash(img, size=16) -> hex ; hamming(a, b) -> int ; content_hash(path, algo="blake2b", digest=32)
compress_to_limit(src, limit_bytes, *, lossless_only=False) -> Packed(ok, data, strategy, note, fmt)
```

`src` is a path, bytes or an open file. `phash` is a pure-Python DCT (no numpy, no imagehash) verified equal to `imagehash.phash` on random images; `dhash`
is Argus' 256-bit hex difference hash. The two measures are different: never compare one with the other. `compress_to_limit` is Kafka's ladder: lossless,
then fewer colours / lower quality, then smaller; animated images are left untouched (`strategy="animated"`). `strip_exif` raises `ValueError` for formats Pillow
cannot rewrite (HEIC, GIF) unless `strict=False`, in which case the original bytes come back; it never silently pretends to have removed a location.

## `hoard_link.docs.archives`

```python
safe_stem(name, default="document", *, max_len=110) ; safe_filename(name, default="file", *, max_len=120) ; write_unique(dir, name, data) -> Path   # "x (2).pdf" on collision, exclusive create
unique_path(directory, name) -> Path ; safe_member(base, name) -> Path     # zip-slip guard: rejects absolute paths and ".."
check_zip, ZipBombError                                              # re-exported from readers_lite
```

## What it replaces, and how each app migrates

Pure swaps: delete the local copy, import the commons function, keep the call sites. Behaviour changes are listed so nobody is surprised.

| App | Local code replaced (paths under the app folder) | Migrate to |
|---|---|---|
| **Borges** | `borges/search.py` (`fold`, `STOPWORDS`, `stem`, `fts_query`, `highlight`, `rrf`), `borges/chunking.py` (`chunk_units`), `borges/queries.py::citation`, `borges/extract/{docx,html,text}.py` | `textsearch`, `chunking` (same algorithm, `CHUNK_VERSION` 3 stays, so indexes do not rebuild), `citations.cite("page"/"doc")`, `readers_lite`, `vecmath.rrf/topk/pack_vec`. Its embeddings BLOBs are already float32 |
| **Argus** | `argus/queries.py::fts_query` and the IDF scoring in `argus/search.py`, `argus/images.py` (`dhash`, `hamming`) | `textsearch.fts_ladder` + `bm25_rescore` (smoothed IDF is the Argus formula), `imaging.dhash/hamming` (same hex format, no re-hash of stored values) |
| **Funes** | `funes_hoard/audio_memory/store.py::fts_query` | `textsearch.fts_ladder`, `highlight` |
| **Echo** | `echo/search.py::_fts_query` | `textsearch.fts_ladder` |
| **Kafka** | `kafka_hoard/readers.py` (`sniff`, `decode_text`, `clean_text`, `split_pages`, docx/html/eml/pdf), `kafka_hoard/store.py::fts_query`, `workshop/names.py` (`safe_stem`, `write_unique`), `workshop/ranges.py`, `workshop/imagetools.py` | `sniff`, `readers_lite`, `textclean`, `textsearch`, `archives`, `pageranges.parse_ranges`, `imaging.compress_to_limit/strip_exif/open_oriented`. Kafka keeps its OCR and heavy extractors on top of `read_any` |
| **Hypatia** | `hypatia/notebook/sources.py` (`_read_text_file`, `_docx_blocks`, `_unstack_words`, `strip_repeated_lines`), `hypatia/notebook/retrieval.py` (`pack_vec`, `cosine`, `fts_query`, `citation`), `hypatia/bank.py::_fts_query`, `src/services/pdfTools/split.ts::parsePageRanges` | `readers_lite`, `textclean`, `vecmath` (same `array('f')` BLOB), `textsearch`, `citations`; the TypeScript `parsePageRanges` becomes a call to `docs.js` `parseRanges`. Behaviour change: invalid entries are now errors, not silently dropped |
| **Vulcan** | `vulcan/search.py::fts_query` | `textsearch.fts_ladder` |
| **Vitruvius** | `vitruvius_hoard/library.py` (`_fts_query`, `_cite`), `ingest/markdown.py`, `dense.py::rrf` | `textsearch`, `citations.cite`, `chunking.chunk_markdown`, `vecmath.rrf/minmax_fuse` |
| **Scheherazade** | `scheherazades_hoard/store.py::_fts_query` | `textsearch.fts_ladder` |
| **Cicero** | `cicero_hoard/extract.py` (`decode_text`, `_check_zip`, docx tables, pptx), `generate.py::chunk_text` | `textclean.decode_text`, `readers_lite.check_zip/read_docx/read_pptx`, `chunking.chunk_text` |
| **Pygmalion** | `pygmalion_hoard/datasets/sources.py` (`chunk_text`, `_read_text_file`) | `chunking.chunk_text`, `readers_lite.read_any` / `textclean.decode_text` |
| **Daguerre** | `daguerre_hoard/phash.py::compute_phash`, `thumbnails.py`, `metadata.py`, `hashing.py`, `formats.py` | `imaging.phash` (bit-identical to imagehash, so stored hashes stay valid), `thumbnail`, `read_exif`, `content_hash`, `open_oriented`, `register_heif` |
| **Faustus** | `pdf_ops.parse_page_ranges`, `gallery_helpers._extract_exif` / `strip_location_exif`, `_extract_docx_native`, `bm25_scores` copies, `pdf_page_evidence_ref` text, the ingest / upload type tables | `pageranges.parse_ranges(allow_reversed=True)` (keeps its swap-and-keep-order behaviour), `imaging.read_exif/strip_exif`, `readers_lite.read_docx`, `textsearch.bm25_rescore`, `citations.cite`, `sniff` |
| **Babel** | `babels_hoard/search.py::_tokenize_for_query` | `textsearch.fts_query` (keeps its trigram table; only the word table query changes) |
| **Galton** | `galton_hoard/services.py`, `api/runs.py` inline file-type checks | `sniff` |
| **Gepetto** | `card_pose_vision` image checks, `backend/` thumbnail and orient boilerplate | `sniff`, `imaging.open_oriented/thumbnail` |
| **Plato** | silhouette image open / thumbnail code | `imaging.open_oriented/thumbnail` |
| **People, Cook, Writers** (Node/TypeScript) | their own FTS-style `MATCH` strings, upload name and type checks, page-range parsing | `js/hoard-commons/docs.js`: `ftsQuery`, `ftsLadder`, `sniff`, `parseRanges`, `highlight`. Nothing to port from Python; the vectors guarantee the same answers |
| **Laplace and Nightingale** | their CSV/XLSX loaders (`laplaces_hoard/engines/data.py`, `nightingale/workbench/engine.py`, `nightingale/services.py`) | **Not replaced.** They read tabular data into typed columns and engines, which is a different job from "text of a spreadsheet for search". Only `sniff` (to route an upload) and `archives.check_zip` / `safe_member` (xlsx zip-bomb and zip-slip guards) are shared. Treat the tabular loaders as a fork that may later become `hoard_link.tabular`; do not grow `read_xlsx` into one |

Migration checklist per app: add the import, run the app's own search and ingest tests, compare a few real queries before and after
(`fts_ladder` returns the same hits as the old query in the first rung and more in the later ones), then delete the local copy in the same change.

## Deviations from the original app copies

* `tokens()` does not drop stopwords unless `stop=` is passed (use `content_words` for that).
* `overlap` is clamped to `size // 2` (the old chunkers could loop for a long time on `overlap >= size`).
* `parse_ranges` has `allow_reversed` (Faustus behaviour); the default rejects reversed ranges (Kafka behaviour).
* `cite` has an added `book` kind; English output uses curly quotes and `ch.`.
* `readers_lite` also reads pptx, xlsx, epub and eml in pure stdlib; `read_any` never raises and adds `mime` and `error`.
* `sniff` has extra kinds (`ole`, `rtf`, `eml`, `sqlite`, `parquet`, `archive`).
* `strip_exif` raises on formats it cannot rewrite unless `strict=False`.
