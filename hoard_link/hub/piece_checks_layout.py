"""Rendered web checks and structural and rendered slide checks."""

from collections import Counter
from contextlib import contextmanager, nullcontext
from itertools import combinations
from pathlib import Path, PurePosixPath
import re
import tempfile
import unicodedata
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile
import xml.etree.ElementTree as ET
import posixpath

from .. import proc
from .piece_checks_sources import resolve_many

_WEB_MEASURES = r"""() => {
  const select = (element) => {
    if (element.id) return '#' + CSS.escape(element.id);
    const parts = [];
    for (let e = element; e && e !== document.body; e = e.parentElement) {
      if (e.id) { parts.unshift('#' + CSS.escape(e.id)); break; }
      const index = Array.from(e.parentElement.children).indexOf(e) + 1;
      parts.unshift(e.localName + ':nth-child(' + index + ')');
    }
    return parts.join(' > ') || 'body';
  };
  const visible = (e) => e.getClientRects().length && getComputedStyle(e).visibility === 'visible';
  const name = (e) => e.getAttribute('aria-label')?.trim() || e.getAttribute('aria-labelledby')?.trim().split(/\s+/)
    .some(id => document.getElementById(id)?.textContent.trim()) ||
    Array.from(e.labels || []).some(label => label.textContent.trim()) ||
    e.textContent.trim() || e.querySelector('img[alt]:not([alt=""])') ||
    e.getAttribute('title')?.trim() || (e.type === 'submit' && e.value);
  const result = {accessibility: [], overflow: [], page_overflow_elements: [], overlaps: [], images: [],
    fragments: [], links: []};
  const viewportWidth = innerWidth;
  const pageWide = document.documentElement.scrollWidth > viewportWidth + 1;
  const overflowing = new Map();
  const reportPageOverflow = (e, rect, source) => {
    if (!pageWide || rect.right <= viewportWidth + 1 || !rect.width || !rect.height) return;
    let edge = rect.right;
    for (let parent = e; parent && parent !== document.documentElement; parent = parent.parentElement) {
      const style = getComputedStyle(parent);
      if (style.position === 'fixed') return;
      if ((parent !== e || source === 'text') && /^(hidden|clip|auto|scroll)$/.test(style.overflowX)) {
        const box = parent.getBoundingClientRect();
        edge = Math.min(edge, box.left + parent.clientLeft + parent.clientWidth);
        if (edge <= viewportWidth + 1) return;
      }
    }
    const via = select(e);
    if (edge > (overflowing.get(e)?.right_px || 0)) {
      overflowing.set(e, {selector: via, source, right_px: Math.round(edge * 100) / 100,
        beyond_px: Math.round((edge - viewportWidth) * 100) / 100});
    }
  };
  if (pageWide) {
    for (const e of [document.body, ...document.body.querySelectorAll('*')]) {
      if (visible(e)) reportPageOverflow(e, e.getBoundingClientRect(), 'element');
    }
  }
  if (!document.documentElement.lang) result.accessibility.push({selector: 'html', rule: 'html-lang'});
  if (!document.title.trim()) result.accessibility.push({selector: 'title', rule: 'document-title'});
  for (const e of document.querySelectorAll('a[href],button,input,select,textarea,img,[role=button]')) {
    if (!visible(e) || e.type === 'hidden') continue;
    const selector = select(e);
    if (e.tagName === 'IMG') {
      if (!e.closest('[aria-hidden=true]') && !e.hasAttribute('alt'))
        result.accessibility.push({selector, rule: 'img-alt'});
      if (e.complete && !e.naturalWidth && (e.currentSrc || e.getAttribute('src')))
        result.images.push({selector, src: e.currentSrc || e.getAttribute('src')});
    } else if (!e.closest('[aria-hidden=true]') && !name(e)) {
      result.accessibility.push({selector, rule: 'accessible-name'});
    }
  }
  for (const a of document.querySelectorAll('a[href]')) {
    let url;
    try { url = new URL(a.href); } catch { continue; }
    if (url.pathname === location.pathname && url.origin === location.origin && url.hash) {
      let id;
      try { id = decodeURIComponent(url.hash.slice(1)); } catch { id = url.hash.slice(1); }
      if (!document.getElementById(id) && !document.getElementsByName(id).length)
        result.fragments.push({selector: select(a), href: a.getAttribute('href')});
    } else if (url.protocol === 'http:' || url.protocol === 'https:') {
      result.links.push({selector: select(a), target: url.href,
        href: a.getAttribute('href'), local: url.origin === location.origin});
    }
  }
  const boxes = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node; node = walker.nextNode();) {
    const e = node.parentElement;
    if (!node.textContent.trim() || !e || !visible(e) ||
        e.closest('script,style,option,[aria-hidden=true]')) continue;
    const range = document.createRange();
    range.selectNodeContents(node);
    for (const rect of range.getClientRects()) {
      if (!rect.width || !rect.height) continue;
      reportPageOverflow(e, rect, 'text');
      const selector = select(e);
      for (let parent = e; parent && parent !== document.body; parent = parent.parentElement) {
        const style = getComputedStyle(parent);
        const box = parent.getBoundingClientRect();
        const clipX = /^(hidden|clip)$/.test(style.overflowX);
        const clipY = /^(hidden|clip)$/.test(style.overflowY);
        if ((clipX && (rect.left < box.left - 2 || rect.right > box.right + 2)) ||
            (clipY && (rect.top < box.top - 2 || rect.bottom > box.bottom + 2))) {
          result.overflow.push({selector, container: select(parent), text: node.textContent.trim().slice(0, 80)});
          break;
        }
      }
      boxes.push({e, rect, selector});
    }
  }
  for (const [e, item] of overflowing) {
    // Report the first crossing, and a descendant if it pushes the edge farther out.
    let covered = false;
    for (let parent = e.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
      if ((overflowing.get(parent)?.right_px || 0) >= item.right_px - 1) { covered = true; break; }
    }
    if (!covered) result.page_overflow_elements.push(item);
  }
  result.page_overflow_elements.sort((a, b) => b.beyond_px - a.beyond_px);
  boxes.sort((a, b) => a.rect.top - b.rect.top);
  const pairs = new Set();
  for (let i = 0; i < boxes.length; i++) {
    const a = boxes[i];
    for (let j = i + 1; j < boxes.length && boxes[j].rect.top < a.rect.bottom - 3; j++) {
      const b = boxes[j];
      if (a.e.contains(b.e) || b.e.contains(a.e)) continue;
      const x = Math.min(a.rect.right, b.rect.right) - Math.max(a.rect.left, b.rect.left);
      const y = Math.min(a.rect.bottom, b.rect.bottom) - Math.max(a.rect.top, b.rect.top);
      const pair = [a.selector, b.selector].sort();
      if (x > 3 && y > 3 && x * y > .18 * Math.min(a.rect.width * a.rect.height, b.rect.width * b.rect.height)
          && !pairs.has(pair.join('|'))) {
        pairs.add(pair.join('|'));
        result.overlaps.push(pair);
      }
    }
  }
  return result;
}"""


@contextmanager
def _web_root(path):
    if path.suffix.lower() != ".zip" or not path.is_file():
        yield path
        return
    with tempfile.TemporaryDirectory(prefix="hub-piece-web-") as folder:
        try:
            with ZipFile(path) as archive:
                files = [member for member in archive.infolist() if not member.is_dir()]
                names = {member.filename for member in files}
                parents = {name.split("/", 1)[0] for name in names}
                wrapper = (next(iter(parents)) + "/" if len(parents) == 1 and not parents & names
                           and "public" not in parents else "")
                if wrapper + "public/index.html" in names:
                    prefix = wrapper + "public/"
                elif wrapper + "index.html" in names:
                    prefix = wrapper
                else:
                    raise ValueError("The ZIP needs index.html at its root or in public/")
                files = [member for member in files if member.filename.startswith(prefix)]
                if len(files) > 10000 or sum(member.file_size for member in files) > 512 * 1024 * 1024:
                    raise ValueError("The web package exceeds the file or byte limit")
                seen = set()
                for member in files:
                    name = member.filename
                    parts = PurePosixPath(name).parts
                    key = tuple(part.casefold() for part in parts)
                    if (PurePosixPath(name).as_posix() != name or name.startswith("/") or
                            not parts or ".." in parts or re.search(r'[:\\<>|?*"]', name) or key in seen or
                            any(part.endswith((".", " ")) for part in parts)):
                        raise ValueError("The web package contains an invalid or duplicate path")
                    seen.add(key)
                    archive.extract(member, folder)
        except BadZipFile as exc:
            raise ValueError("The web package is not a readable ZIP") from exc
        yield Path(folder) / prefix


def check_web(path, requirements, report):
    with _web_root(path) as root:
        _check_web_page(root, requirements, report)


class _LocalOnlyEngine:
    """Browser engine whose pages abort every request that does not go to the local page server.

    A confined worker has no network: the Hub must not reach out for it while rendering its page.
    """

    def __init__(self, engine, base):
        self._engine, self._base = engine, base.rstrip("/") + "/"

    def new_page(self, **options):
        page = self._engine.new_page(**{**options, "service_workers": "block"})
        page.route("**/*", lambda route: route.continue_() if (
            route.request.url.startswith(self._base) or route.request.url.split(":", 1)[0] in ("data", "blob", "about")
        ) else route.abort())
        # Without connect_to_server the handler answers for the server: nothing leaves the machine.
        page.route_web_socket("**/*", lambda socket: None)
        return page

    def __getattr__(self, name):
        return getattr(self._engine, name)


def _offline_flags(base):
    """Chromium switches that leave a page nothing to reach but its own server.

    Request interception (``_LocalOnlyEngine``) never sees what the browser does before or beside a request:
    ``<link rel=preconnect>`` and ``dns-prefetch`` open a connection or resolve a name, and a subdomain is
    enough to carry data out. So the browser itself has no network: every name but the loopback address
    resolves to nothing, and every connection goes to a proxy that does not exist. The implicit loopback
    exemption of the proxy is withdrawn, leaving only the page server's own address and port.
    """
    return ["--proxy-server=http://proxy.invalid:9",
            f"--proxy-bypass-list=<-loopback>;{urlsplit(base).netloc}",
            "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--dns-prefetch-disable"]


@contextmanager
def _offline_launch(browser):
    """Start the page engine's browser with ``_offline_flags`` (the engine reads its switches when it launches)."""
    from vitruvius_hoard import browser as engine

    original = engine.launch_args
    engine.launch_args = lambda: [*original(), *_offline_flags(browser._base)]
    try:
        yield
    finally:
        engine.launch_args = original


def _check_web_page(path, requirements, report):
    from vitruvius_hoard.functional_check.browser import state_facts
    from vitruvius_hoard.functional_check.run import Browser, aimed

    folder, entry = aimed(path, "index.html")
    if not (folder / entry).is_file():
        raise ValueError("The web piece has no entry page")
    viewports = requirements.get("viewports", [[1440, 900], [390, 844]])
    if not viewports or len(viewports) > 8 or any(len(size) != 2 or min(size) < 1 for size in viewports):
        raise ValueError("Declare one to eight positive [width, height] viewport sizes")
    rows = []
    link_results = {}
    offline = bool(requirements.get("_offline"))
    page_engine = Browser(folder, entry)
    with (_offline_launch(page_engine) if offline else nullcontext()), page_engine as browser:
        if offline:
            browser._engine = _LocalOnlyEngine(browser._engine, browser._base)
        for width, height in viewports:
            with browser.open({"viewport": {"width": width, "height": height}}) as (page, crashes, console, failed):
                for image in page.locator("img[loading=lazy]").all():
                    image.evaluate("(img) => { img.loading = 'eager'; }")
                page.evaluate("() => Promise.race([Promise.allSettled([...document.images].map(i => i.decode())),"
                              " new Promise(resolve => setTimeout(resolve, 1200))])")
                facts = page.evaluate(_WEB_MEASURES)
                row = {"size_px": [width, height], "page_overflow_px": state_facts(page)["overflow_px"],
                       "runtime_errors": [*crashes, *console], "failed_requests": list(failed), **facts}
                for link in row["links"]:
                    target = link["target"]
                    if link["local"] and target not in link_results:
                        try:
                            response = page.request.head(target, timeout=3000)
                            if response.status in (405, 501):
                                response = page.request.get(target, timeout=3000)
                            link_results[target] = {"status": response.status, "resolved": response.status < 400}
                            response.dispose()
                        except Exception as exc:
                            link_results[target] = {"resolved": None, "error": str(exc).splitlines()[0]}
                rows.append(row)
    external = resolve_many([link["target"] for row in rows for link in row["links"] if not link["local"]],
                            folder, requirements.get("resolve_remote", True), requirements.get("_scope"))
    for row in rows:
        for link in row["links"]:
            result = (link_results if link["local"] else external)[link.pop("target")]
            link.update({key: value for key, value in result.items() if key != "target"})
    report.measures.update(viewports=rows, engine="vitruvius.functional_check")
    rules = {
        "accessibility": ("Basic accessible names, language and image alternatives.",
                          "Comprueba nombres accesibles, idioma y texto alternativo."),
        "overlaps": ("Find overlapping visible text boxes.", "Localiza cajas de texto visible que se solapan."),
        "images": ("Find images the browser cannot decode.", "Localiza imágenes que el navegador no puede cargar."),
        "runtime_errors": ("Check browser errors.", "Comprueba los errores del navegador."),
        "failed_requests": ("Find failed resource requests.", "Localiza peticiones de recursos fallidas."),
    }
    for key, (en, es) in rules.items():
        failures = [{"size_px": row["size_px"], "items": row[key]} for row in rows if row[key]]
        report.add(key.replace("_", "-"), not failures, en, es, details=failures)
    failures = [{"size_px": row["size_px"], "clipped": row["overflow"], "page_px": row["page_overflow_px"],
                 "elements": row["page_overflow_elements"]}
                for row in rows if row["overflow"] or row["page_overflow_px"] > 1]
    report.add("overflow", not failures, "Check clipped text and horizontal page overflow.",
               "Comprueba el texto recortado y el desbordamiento horizontal.", details=failures)
    failures = [{"size_px": row["size_px"], "links": [*row["fragments"],
                 *(link for link in row["links"] if link["resolved"] is not True)]} for row in rows]
    failures = [row for row in failures if row["links"]]
    broken = any(link.get("resolved") is False or "resolved" not in link
                 for row in failures for link in row["links"])
    report.add("links", False if broken else None if failures else True,
               "Resolve page links and same-page anchors.", "Comprueba enlaces y anclas de la página.", details=failures)


def _words(text):
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return Counter(re.findall(r"[^\W_]+", normalized))


def _pdf_words(path):
    import pymupdf

    candidates = [Path(folder) / "LibreOffice/program" for folder in (
        "C:/Program Files", "C:/Program Files (x86)")]
    office = proc.find_exe("soffice", env_vars=("LIBREOFFICE_PATH",), extra_dirs=candidates)
    if not office:
        raise RuntimeError("LibreOffice is unavailable")
    with tempfile.TemporaryDirectory(prefix="hub-piece-slides-") as folder:
        root = Path(folder)
        result = proc.run([office, "-env:UserInstallation=" + (root / "profile").as_uri(), "--headless",
                           "--convert-to", "pdf", "--outdir", folder, str(path)], timeout=25)
        pdf = root / (path.stem + ".pdf")
        if result.returncode or not pdf.is_file():
            raise RuntimeError(proc.tail_lines(result.stderr or result.stdout) or "PPTX rendering failed")
        with pymupdf.open(pdf) as deck:
            return [_words(page.get_text()) for page in deck]


def _external_resources(path):
    """Relationships the office renderer would load from outside the file (hyperlinks are not loaded)."""
    with ZipFile(path) as archive:
        return [name for name in archive.namelist() if name.endswith(".rels") and any(
            rel.get("TargetMode") == "External" and not rel.get("Type", "").endswith("/hyperlink")
            for rel in ET.fromstring(archive.read(name)))]


def _deck_links(path):
    with ZipFile(path) as archive:
        names = set(archive.namelist())
        for entry in sorted(names):
            match = re.search(r"slides/_rels/(slide\d+)\.xml\.rels$", entry)
            if not match:
                continue
            for rel in ET.fromstring(archive.read(entry)):
                kind = rel.get("Type", "").rsplit("/", 1)[-1]
                target = rel.get("Target")
                if kind not in {"hyperlink", "image", "slide"} or not target:
                    continue
                external = rel.get("TargetMode") == "External"
                member = (target.lstrip("/") if target.startswith("/") else
                          posixpath.normpath(posixpath.join("ppt/slides", target)))
                yield {"part": match[1], "id": rel.get("Id"), "target": target, "kind": kind,
                       "resolved": None if external else member in names, "external": external}


def _slide_text(shape):
    if shape.has_text_frame:
        return shape.text
    if shape.has_table:
        return " ".join(cell.text for row in shape.table.rows for cell in row.cells)
    return ""


def check_slides(path, requirements, report):
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    deck = Presentation(str(path))
    size = [int(deck.slide_width), int(deck.slide_height)]
    outside, overlaps, access, text = [], [], [], []
    for index, slide in enumerate(deck.slides, 1):
        boxes = []
        per_shape = []
        if slide.shapes.title is None or not slide.shapes.title.text.strip():
            access.append({"slide": index, "rule": "slide-title-placeholder"})
        for shape in slide.shapes:
            name = shape.name
            left, top = shape.left, shape.top
            width, height = shape.width, shape.height
            if None in (left, top, width, height):
                continue
            right, bottom = left + width, top + height
            content = _slide_text(shape)
            picture = shape.shape_type in (MSO_SHAPE_TYPE.PICTURE, MSO_SHAPE_TYPE.LINKED_PICTURE)
            if picture:
                labels = shape._element.xpath(".//p:cNvPr")
                if labels and not any(label.get("descr") or label.get("title") for label in labels):
                    access.append({"slide": index, "shape": name, "rule": "image-alternative"})
            if not (content.strip() or picture):
                continue
            per_shape.append((name, _words(content)))
            if min(left, top) < -1000 or right > size[0] + 1000 or bottom > size[1] + 1000:
                outside.append({"slide": index, "shape": name, "box_emu": [left, top, right, bottom]})
            boxes.append((name, left, top, right, bottom, picture))
        for (a, l1, t1, r1, b1, p1), (b, l2, t2, r2, b2, p2) in combinations(boxes, 2):
            if p1 != p2:  # an image can be an intentional background for text
                continue
            x, y = min(r1, r2) - max(l1, l2), min(b1, b2) - max(t1, t2)
            if x > 1000 and y > 1000 and x * y > .15 * min((r1-l1)*(b1-t1), (r2-l2)*(b2-t2)):
                overlaps.append({"slide": index, "shapes": [a, b]})
        text.append(per_shape)
    links = list(_deck_links(path))
    external = resolve_many([row["target"] for row in links if row["external"]], path.parent,
                            requirements.get("resolve_remote", True), requirements.get("_scope"))
    for row in links:
        if row["external"]:
            row.update(external[row["target"]])
    report.measures.update(slides=len(deck.slides), size_emu=size, links=links)
    expected = requirements.get("size_emu")
    if expected is not None:
        report.add("size", size == list(expected), "Compare the canvas size.", "Coteja el tamaño del lienzo.",
                   measured=size, expected=expected)
    expected_count = requirements.get("slide_count")
    if expected_count is not None:
        report.add("slide-count", len(text) == expected_count, "Count the slides.", "Cuenta las diapositivas.",
                   measured=len(text), expected=expected_count)
    report.add("accessibility", not access, "Check slide title placeholders and image alternatives.",
               "Comprueba los marcadores de título y el texto alternativo.", details=access)
    report.add("overlaps", not overlaps, "Find overlapping slide boxes of the same kind.",
               "Localiza cajas del mismo tipo que se solapan.", details=overlaps)
    report.add("canvas-overflow", not outside, "Find boxes outside the slide canvas.",
               "Localiza cajas que salen del lienzo.", details=outside)
    pending = [row for row in links if row["resolved"] is not True]
    report.add("links-and-images", False if any(row["resolved"] is False for row in pending) else None if pending else True,
               "Resolve slide hyperlinks and image relationships.",
               "Comprueba los enlaces y las referencias a las imágenes.", details=pending)

    if requirements.get("_offline") and _external_resources(path):
        report.add("rendered-text", None, "The deck links external resources, so it was not rendered.",
                   "La presentación enlaza recursos externos, así que no se ha renderizado.")
        return
    pages = _pdf_words(path)
    missing = []
    for index, shapes in enumerate(text, 1):
        rendered = pages[index - 1] if index <= len(pages) else Counter()
        expected_words = sum((words for _, words in shapes), Counter())
        lost = expected_words - rendered
        for name, words in shapes:
            candidates = words & lost
            if candidates:
                missing.append({"slide": index, "shape": name, "missing_words": dict(candidates)})
    report.add("rendered-text", len(pages) == len(text) and not missing,
               "Locate text absent from the rendered PDF, including possible clipping.",
               "Localiza texto ausente en el PDF renderizado, incluido el que puede estar recortado.",
               rendered_pages=len(pages), details=missing)
