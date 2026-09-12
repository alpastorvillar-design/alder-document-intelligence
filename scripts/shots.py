"""The README's screenshots, taken the same way every time.

Headless Chromium at one width, so the six are consistent with each other and
can be regenerated after any change to the interface instead of being
re-taken by hand.
"""

import pathlib
import subprocess
import sys

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
BASE = "http://127.0.0.1:8000"
OUT = pathlib.Path("docs/img")
WIDTH = 1400

D42 = "2ad8f756-a7b8-4bc6-b433-d17eb6464566"
D41 = "cadd323a-96bc-4622-a4ac-430d6cc14a5c"
EVIDENCE = "8f4b588f-14bf-442a-8fdc-842879bb80e9"

# name, url, how tall to render, how much of it to keep.
#
# The render height only has to exceed the page; the trailing background is
# trimmed afterwards, so it does not have to be tuned per page. `keep` caps
# the result for the long pages, where the point is the top of the screen and
# not all fourteen findings.
SHOTS = [
    ("01-queue", f"{BASE}/ui/dossiers", 1600, None),
    ("02-intake", f"{BASE}/ui/dossiers/new", 1800, None),
    ("03-review", f"{BASE}/ui/dossiers/{D42}", 3200, 1320),
    # El escaneo es un A4 con mucho blanco, asi que el recorte por fondo no
    # encuentra donde parar: se le dice.
    ("04-evidence", f"{BASE}/ui/evidence/{EVIDENCE}", 2400, 1180),
    # El informe del expediente con incidencias: el que ensena el producto
    # trabajando, no el que sale limpio.
    ("05-report", f"{BASE}/dossiers/{D42}/reports/latest.html", 3200, 1320),
]


def trimmed_height(image) -> int:
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

    # Absoluta: Chrome resuelve esta ruta contra su propio directorio.
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
# so the image can be regenerated without a model call, and without asking a
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


def ask_shot() -> None:
    import re
    import urllib.request

    from PIL import Image

    url = f"{BASE}/ui/dossiers/{D42}"
    # A loopback http URL built from constants above.
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
        page = response.read().decode("utf-8")

    styles = "\n".join(re.findall(r"<style>(.*?)</style>", page, re.S))
    drawer = re.search(r'<aside class="copilot"[^>]*>.*?</aside>', page, re.S)
    if drawer is None:
        raise SystemExit("no encuentro el cajon del copiloto en la pagina")
    panel = drawer.group(0).replace('class="copilot"', 'class="copilot open"', 1)
    # The thread and the meter are filled in by the browser; here they are the
    # recorded exchange.
    panel = re.sub(r'(<div class="copilot-thread"[^>]*>)(</div>)', rf"\1{THREAD}\2", panel, count=1)
    panel = re.sub(
        r'(<p class="copilot-meter"[^>]*>).*?(</p>)', rf"\1{METER}\2", panel, count=1, flags=re.S
    )
    # The picker is populated from the status endpoint, so it arrives empty.
    with urllib.request.urlopen(  # noqa: S310
        f"{BASE}/dossiers/{D42}/questions", timeout=60
    ) as response:
        import json

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
    wanted = sys.argv[1:] or [name for name, *_ in SHOTS] + ["06-ask"]
    for name, url, height, keep in SHOTS:
        if name in wanted:
            shoot(name, url, height, keep)
    if "06-ask" in wanted:
        ask_shot()
