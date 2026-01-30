import base64
import os
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel, Field
from jinja2 import Environment, FileSystemLoader, select_autoescape

from weasyprint import HTML, CSS
from weasyprint.urls import default_url_fetcher


# -----------------------------
# Configuration (RGPD / sécurité)
# -----------------------------
API_KEY = os.getenv("API_KEY", "")  # si vide => pas de contrôle de clé
MAX_CHARS = int(os.getenv("MAX_CHARS", "400000"))  # limite pour éviter abus
ALLOW_REMOTE_ASSETS = os.getenv("ALLOW_REMOTE_ASSETS", "false").lower() == "true"
ALLOWED_REMOTE_HOSTS = set(
    h.strip().lower() for h in os.getenv("ALLOWED_REMOTE_HOSTS", "").split(",") if h.strip()
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")

env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "xml"]),
)

app = FastAPI(title="WeasyPrint PDF Backend", version="1.0.0")


# -----------------------------
# Modèles d'entrée
# -----------------------------
class PdfRequest(BaseModel):
    title: str = Field(default="Rapport", max_length=200)
    subtitle: Optional[str] = Field(default=None, max_length=300)
    client: Optional[str] = Field(default=None, max_length=200)
    date: Optional[str] = Field(default=None, max_length=50)

    # Fournir soit content_html, soit content_md
    content_html: Optional[str] = None
    content_md: Optional[str] = None

    # Métadonnées libres (optionnel)
    meta: Optional[Dict[str, Any]] = None


def _check_key(x_api_key: Optional[str]):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _safe_url_fetcher(url: str):
    """
    RGPD + sécurité :
    - Bloque par défaut les URLs http(s) (pas de fuite vers l'extérieur)
    - Autorise data: et file:
    - Option : allowlist de domaines si ALLOW_REMOTE_ASSETS=true
    """
    u = url.lower().strip()

    if u.startswith("data:") or u.startswith("file:"):
        return default_url_fetcher(url)

    if u.startswith("http://") or u.startswith("https://"):
        if not ALLOW_REMOTE_ASSETS:
            raise HTTPException(status_code=400, detail=f"Remote asset blocked: {url}")

        # allowlist simple par hostname si fourni
        if ALLOWED_REMOTE_HOSTS:
            try:
                from urllib.parse import urlparse
                host = (urlparse(url).hostname or "").lower()
            except Exception:
                host = ""
            if host not in ALLOWED_REMOTE_HOSTS:
                raise HTTPException(status_code=400, detail=f"Remote host not allowed: {host}")

        return default_url_fetcher(url)

    # Tout le reste est bloqué
    raise HTTPException(status_code=400, detail=f"Unsupported URL scheme: {url}")


def _markdown_to_html(md: str) -> str:
    # Conversion markdown -> html (simple + robuste)
    from markdown_it import MarkdownIt
    md_parser = MarkdownIt("commonmark", {"html": True, "linkify": True, "typographer": True})
    return md_parser.render(md)


def _render_html(req: PdfRequest) -> str:
    if req.content_html:
        body_html = req.content_html
    elif req.content_md:
        body_html = _markdown_to_html(req.content_md)
    else:
        raise HTTPException(status_code=400, detail="Provide content_html or content_md")

    # garde-fou taille
    if len(body_html) > MAX_CHARS:
        raise HTTPException(status_code=413, detail=f"Content too large (>{MAX_CHARS} chars)")

    template = env.get_template("report.html")
    return template.render(
        title=req.title,
        subtitle=req.subtitle,
        client=req.client,
        date=req.date,
        body_html=body_html,
        meta=req.meta or {},
    )


def _generate_pdf_bytes(html_str: str) -> bytes:
    # CSS local
    css_path = os.path.join(ASSETS_DIR, "styles.css")
    css = CSS(filename=css_path)

    html = HTML(string=html_str, base_url=BASE_DIR, url_fetcher=_safe_url_fetcher)
    return html.write_pdf(stylesheets=[css])


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/generate-pdf")
def generate_pdf(req: PdfRequest, x_api_key: Optional[str] = Header(default=None)):
    """
    Retourne un PDF binaire (Content-Type: application/pdf)
    """
    _check_key(x_api_key)
    html_str = _render_html(req)
    pdf_bytes = _generate_pdf_bytes(html_str)

    filename = f"{req.title.strip().replace(' ', '_')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@app.post("/generate-pdf-json")
def generate_pdf_json(req: PdfRequest, x_api_key: Optional[str] = Header(default=None)):
    """
    Retourne un JSON avec le PDF encodé en base64 (utile pour Actions GPT).
    Attention : gros PDFs => payload volumineux.
    """
    _check_key(x_api_key)
    html_str = _render_html(req)
    pdf_bytes = _generate_pdf_bytes(html_str)

    b64 = base64.b64encode(pdf_bytes).decode("ascii")
    filename = f"{req.title.strip().replace(' ', '_')}.pdf"
    return JSONResponse({"filename": filename, "pdf_base64": b64})
