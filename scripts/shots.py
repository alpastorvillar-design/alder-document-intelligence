"""The README's screenshots, taken the same way every time.

Headless Chromium at one width, so the six are consistent with each other and
can be regenerated after any change to the interface instead of being
re-taken by hand.
"""

import json
import pathlib
import subprocess
import sys
import urllib.parse
import urllib.request
from typing import Any

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
BASE = "http://127.0.0.1:8000"
OUT = pathlib.Path("docs/img")
WIDTH = 1400


def api_json(path: str) -> Any:
    """Read one JSON response from the local, synthetic demonstration."""

    with urllib.request.urlopen(f"{BASE}{path}", timeout=60) as response:  # noqa: S310
        return json.load(response)


def dossier_id(reference: str) -> str:
    """Resolve a stable business reference to the run-specific UUID."""

    encoded = urllib.parse.quote(reference, safe="")
    matches = api_json(f"/dossiers?reference={encoded}")
    if len(matches) != 1:
        raise SystemExit(f"expected one dossier for {reference}, found {len(matches)}")
    return str(matches[0]["id"])


def evidence_id(reference: str, filename: str, field_path: str) -> str:
    """Find the evidence row without relying on UUIDs from an earlier run."""

    current_dossier = dossier_id(reference)
    documents = api_json(f"/dossiers/{current_dossier}/documents")
    matching_documents = [
        document for document in documents if document["original_filename"] == filename
    ]
    if len(matching_documents) != 1:
        raise SystemExit(f"expected one {filename} in {reference}, found {len(matching_documents)}")
    document_id = matching_documents[0]["id"]
    extractions = api_json(f"/dossiers/{current_dossier}/extractions")
    matching_extractions = [
        extraction
        for extraction in extractions
        if extraction["document_id"] == document_id and extraction["field_path"] == field_path
    ]
    if len(matching_extractions) != 1:
        raise SystemExit(
            f"expected one {field_path} in {filename}, found {len(matching_extractions)}"
        )
    return str(matching_extractions[0]["id"])


def shot_plan() -> list[tuple[str, str, int, int | None]]:
    """Build URLs from data created by this run, not from an old database."""

    dossier_42 = dossier_id("INN-2025-042")
    evidence = evidence_id("INN-2025-041", "justificante-02-FS-2025-0588.jpg", "invoice.total_eur")
    # name, url, how tall to render, how much of it to keep.  The render height
    # exceeds the page; trailing background is trimmed afterwards. `keep` caps
    # long pages where the point is the top, not every finding.
    return [
        ("01-queue", f"{BASE}/ui/dossiers", 1600, None),
        ("02-intake", f"{BASE}/ui/dossiers/new", 1800, None),
        ("03-review", f"{BASE}/ui/dossiers/{dossier_42}", 3200, 1320),
        # An A4 scan has too much white for background trimming to find its end.
        ("04-evidence", f"{BASE}/ui/evidence/{evidence}", 2400, 1180),
        # The dossier with seeded findings shows the product working.
        ("05-report", f"{BASE}/dossiers/{dossier_42}/reports/latest.html", 3200, 1320),
    ]


def trimmed_height(image: Any) -> int:
    """Where the page stops, so a screenshot is not mostly background.

    The render height is deliberately generous - a page has to fit in it - and
    what is left over is a band of the body colour. Compared against the
    bottom-left pixel rather than a hard-coded colour, so it works in either
    theme.
    """
    background = image.getpixel((2, image.height - 2))
    for y in range(image.height - 1, 0, -1):
        row = [image.getpixel((x, y)) for x in range(0, image.width, 7)]
        if any(pixel != background for pixel in row):
            return min(y + 24, image.height)
    return image.height


def shoot(name: str, url: str, height: int, keep: int | None) -> None:
    from PIL import Image

    # Chrome exits successfully even when the server renders a JSON 404.  A
    # preflight prevents that error page from silently replacing a published
    # product screenshot.
    with urllib.request.urlopen(url, timeout=60):  # noqa: S310
        pass

    # Absolute: Chrome otherwise resolves the path against its own directory.
    raw = (OUT / f"{name}.raw.png").resolve()
    # Every argument is a constant in this file and the binary is a fixed
    # path, so there is no untrusted input to check.
    subprocess.run(  # noqa: S603
        [
            CHROME,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            "--force-dark-mode",
            f"--window-size={WIDTH},{height}",
            f"--screenshot={raw}",
            url,
        ],
        check=True,
        capture_output=True,
    )
    image = Image.open(raw).convert("RGB")
    height = trimmed_height(image)
    if keep:
        height = min(height, keep)
    image = image.crop((0, 0, image.width, height))
    final = OUT / f"{name}.png"
    image.save(final)
    raw.unlink()
    print(f"  {final.name}  {image.size[0]}x{image.size[1]}  {final.stat().st_size // 1024} KB")


# The copilot drawer is the one screen a screenshot cannot simply visit: the
# panel opens from `localStorage` and its content only exists after somebody
# asks something. So the page is fetched, the drawer is lifted out of it with
# its stylesheet, and one exchange is put back in.
#
# The exchange is real - this is what `qwen3.5:9b` answered on INN-2025-042,
# with the citation it returned and the token count it reported - recorded here
# so the image can be recreated without a model call, and without asking a
# question that would cost money on a metered backend.
ANSWER = (
    "Según la memoria técnica (E1), hay 3 personas con dedicación al "
    "proyecto: Nerea Talvi, Iker Rondel y Marta Uxeli."
)
QUESTION = "¿Cuántas personas tienen dedicación al proyecto?"
CITATION = ("E1", "memoria-tecnica-copia.pdf", "PDF · página 1, caracteres 0-871")
TRACE = "ollama · qwen3.5:9b · hybrid · 1543 tokens"
METER = (
    "Hoy: <b>0</b> tokens en la nube · <b>4641</b> tokens en local, "
    "3 consulta(s), sin coste · se reinicia en <b>21 h 8 min</b>"
)

THREAD = f"""<div class="turn">
<p class="asked">{QUESTION}</p>
<p class="stance enough">La evidencia sostiene la respuesta</p>
<p class="said">{ANSWER}</p>
<ul class="cites"><li><details class="cite"><summary>
  <span class="tag">{CITATION[0]}</span>
  <span class="doc">{CITATION[1]}</span>
  <span class="where">{CITATION[2]}</span>
</summary></details></li></ul>
<p class="trace">{TRACE}</p>
</div>"""


def ask_shot(dossier_42: str) -> None:
    import re
    import urllib.request

    from PIL import Image

    url = f"{BASE}/ui/dossiers/{dossier_42}"
    # A loopback http URL built from constants above.
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
        page = response.read().decode("utf-8")

    styles = "\n".join(re.findall(r"<style>(.*?)</style>", page, re.S))
    drawer = re.search(r'<aside class="copilot"[^>]*>.*?</aside>', page, re.S)
    if drawer is None:
        raise SystemExit("the copilot drawer is missing from the page")
    panel = drawer.group(0).replace('class="copilot"', 'class="copilot open"', 1)
    # The thread and the meter are filled in by the browser; here they are the
    # recorded exchange.
    panel = re.sub(r'(<div class="copilot-thread"[^>]*>)(</div>)', rf"\1{THREAD}\2", panel, count=1)
    panel = re.sub(
        r'(<p class="copilot-meter"[^>]*>).*?(</p>)', rf"\1{METER}\2", panel, count=1, flags=re.S
    )
    # The picker is populated from the status endpoint, so it arrives empty.
    with urllib.request.urlopen(  # noqa: S310
        f"{BASE}/dossiers/{dossier_42}/questions", timeout=60
    ) as response:
        status = json.load(response)
    options = "".join(
        f"<option{' selected' if model['id'].endswith('qwen3.5:9b') else ''}>"
        f"{'🖥 ' if model.get('local') else '☁ '}{model['label']}"
        f"{' · ' + model['note'] if model.get('local') and model.get('note') else ''}</option>"
        for model in status["models"]
    )
    panel = re.sub(
        r'(<select id="copilot-model"[^>]*>)(</select>)', rf"\1{options}\2", panel, count=1
    )
    panel = panel.replace(
        '<select id="copilot-mode" aria-label="Modo de recuperación"></select>',
        '<select id="copilot-mode"><option>Léxica</option><option>Vectorial</option>'
        "<option selected>Híbrida</option></select>",
    )
    panel = panel.replace(
        '<p class="copilot-state" id="copilot-state">comprobando…</p>',
        '<p class="copilot-state on" id="copilot-state">Embeddings «ollama», modelo aprendido.</p>',
    )

    document = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<style>{styles}</style>
<style>
  /* In the page the drawer is fixed and slid in from the right; here it is
     the document, so the transform and the viewport height come off. */
  html, body {{ margin: 0; background: var(--paper); }}
  .copilot {{ position: static !important; transform: none !important;
              width: 760px !important; inset: auto !important;
              border-left: 0 !important; box-shadow: none !important; }}
  .copilot-thread {{ overflow: visible !important; }}
  * {{ transition: none !important; animation: none !important; }}
</style></head><body>{panel}</body></html>"""

    page_file = (OUT / "06-ask.html").resolve()
    page_file.write_text(document, encoding="utf-8")
    raw = (OUT / "06-ask.raw.png").resolve()
    # Every argument is a constant in this file and the binary is a fixed
    # path, so there is no untrusted input to check.
    subprocess.run(  # noqa: S603
        [
            CHROME,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            "--force-dark-mode",
            "--window-size=760,1400",
            f"--screenshot={raw}",
            page_file.as_uri(),
        ],
        check=True,
        capture_output=True,
    )
    image = Image.open(raw).convert("RGB")
    image = image.crop((0, 0, image.width, trimmed_height(image)))
    final = OUT / "06-ask.png"
    image.save(final)
    raw.unlink()
    page_file.unlink()
    print(f"  {final.name}  {image.size[0]}x{image.size[1]}  {final.stat().st_size // 1024} KB")


if __name__ == "__main__":
    shots = shot_plan()
    wanted = sys.argv[1:] or [name for name, *_ in shots] + ["06-ask"]
    for name, url, height, keep in shots:
        if name in wanted:
            shoot(name, url, height, keep)
    if "06-ask" in wanted:
        ask_shot(dossier_id("INN-2025-042"))
