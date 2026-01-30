import base64
import os
import time
import uuid
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import FastAPI, Header, HTTPException, BackgroundTasks
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel, Field
from jinja2 import Environment, FileSystemLoader, select_autoescape

from weasyprint import HTML, CSS
from weasyprint.urls import default_url_fetcher


# -----------------------------
# Configuration (sécurité / RGPD)
# -----------------------------
API_KEY = os.getenv("API_KEY", "")  # si vide => pas de contrôle
MAX_CHARS = int(os.getenv("MAX_CHARS", "400000"))  # limite de taille HTML
ALLOW_REMOTE_ASSETS = os.getenv("ALLOW_REMOTE_ASSETS", "false").lower() == "true"
ALLOWED_REMOTE_HOSTS = set(
    h.strip().lower() for h in os.getenv("ALLOWED_REMOTE_HOSTS", "").split(",") if h.strip()
)

# URL publique de ton service (pour construire le lien pdf_url)
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://auditreportgen.enstso.com").rstrip("/")

# Stockage temporaire PDF (prévoir un volume Coolify)
PDF_STORE_DIR = os.getenv("PDF_STORE_DIR", "/data/pdfs")
PDF_TTL_SECONDS = int(os.getenv("PDF_TTL_SECONDS", "3600"))  # 1h
DELETE_AFTER_FIRST_DOWNLOAD = os.getenv("DELETE_AFTER_FIRST_DOWNLOAD", "false").lower() == "true"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
ASSETS_DIR = os.path.join(BASE_DIR, "assets")

env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "xml"]),
)

app = FastAPI(title="AuditReportGen PDF Backend", version="1.2.0")


# -----------------------------
# Modèle d'entrée
# -----------------------------
class PdfRequest(BaseModel):
    title: str = Field(default="Rapport", max_length=200)
    subtitle: Optional[str] = Field(default=None, max_length=300)
    client: Optional[str] = Field(default=None, max_length=200)
    date: Optional[str] = Field(default=None, max_length=50)

    # fournir soit HTML soit Markdown
    content_html: Optional[str] = None
    content_md: Optional[str] = None

    meta: Optional[Dict[str, Any]] = None


# -----------------------------
# Helpers sécurité / stockage
# -----------------------------
def _check_key(x_api_key: Optional[str]):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _ensure_store():
    Path(PDF_STORE_DIR).mkdir(parents=True, exist_ok=True)


def _cleanup_expired():
    """Supprime les PDFs expirés (best-effort)."""
    _ensure_store()
    now = time.time()
    for p in Path(PDF_STORE_DIR).glob("*.pdf"):
        try:
            if now - p.stat().st_mtime > PDF_TTL_SECONDS:
                p.unlink(missing_ok=True)
        except Exception:
            # best-effort: ne pas faire échouer un PDF à cause du cleanup
            pass


def _safe_url_fetcher(url: str):
    """
    Sécurité / RGPD :
    - Autorise data: et file:
    - Bloque http(s) par défaut (pas de fuite vers Internet)
    - Option: ALLOW_REMOTE_ASSETS=true et allowlist ALLOWED_REMOTE_HOSTS
    """
    u = (url or "").strip()
    ul = u.lower()

    if ul.startswith("data:") or ul.startswith("file:"):
        return default_url_fetcher(u)

    if ul.startswith("http://") or ul.startswith("https://"):
        if not ALLOW_REMOTE_ASSETS:
            raise ValueError(f"Remote asset blocked: {u}")

        # allowlist simple (host exact)
        if ALLOWED_REMOTE_HOSTS:
            from urllib.parse import urlparse
            host = (urlparse(u).hostname or "").lower()
            if host not in ALLOWED_REMOTE_HOSTS:
                raise ValueError(f"Remote host not allowed: {host}")

        return default_url_fetcher(u)

    raise ValueError(f"Unsupported URL scheme: {u}")


def _markdown_to_html(md: str) -> str:
    from markdown_it import MarkdownIt
    md_parser = MarkdownIt("commonmark", {"html": True, "linkify": True, "typographer": True})
    return md_parser.render(md)


def _render_html(req: PdfRequest) -> str:
    if req.content_html and req.content_md:
        # on laisse passer mais on priorise HTML pour éviter ambiguïté
        body_html = req.content_html
    elif req.content_html:
        body_html = req.content_html
    elif req.content_md:
        body_html = _markdown_to_html(req.content_md)
    else:
        raise HTTPException(status_code=400, detail="Provide content_html or content_md")

    if len(body_html) > MAX_CHARS:
        raise HTTPException(status_code=413, detail=f"Content too large (>{MAX_CHARS} chars)")

    # Template obligatoire
    try:
        template = env.get_template("report.html")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Template not found: {e}")

    return template.render(
        title=req.title,
        subtitle=req.subtitle,
        client=req.client,
        date=req.date,
        body_html=body_html,
        meta=req.meta or {},
    )


def _load_css() -> CSS:
    css_path = os.path.join(ASSETS_DIR, "styles.css")
    if os.path.exists(css_path):
        return CSS(filename=css_path)

    # fallback minimal si styles.css absent
    fallback = """
    @page { size: A4; margin: 18mm 16mm; }
    body { font-family: DejaVu Sans, Arial, sans-serif; font-size: 10.5pt; line-height: 1.45; color: #111; }
    img { max-width: 100%; height: auto; object-fit: contain; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border: 1px solid #ddd; padding: 6pt; vertical-align: top; }
    """
    return CSS(string=fallback)


def _generate_pdf_bytes(html_str: str) -> bytes:
    css = _load_css()
    html = HTML(string=html_str, base_url=BASE_DIR, url_fetcher=_safe_url_fetcher)
    return html.write_pdf(stylesheets=[css])


def _safe_filename(title: str) -> str:
    # simplification : évite caractères problématiques
    keep = []
    for ch in (title or "rapport"):
        if ch.isalnum() or ch in (" ", "_", "-", "."):
            keep.append(ch)
    name = "".join(keep).strip().replace(" ", "_")
    return (name[:80] if name else "rapport") + ".pdf"


# -----------------------------
# Routes
# -----------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/generate-pdf")
def generate_pdf(req: PdfRequest, x_api_key: Optional[str] = Header(default=None)):
    """
    Retour binaire application/pdf
    """
    _check_key(x_api_key)
    try:
        html_str = _render_html(req)
        pdf_bytes = _generate_pdf_bytes(html_str)
    except HTTPException:
        raise
    except Exception as e:
        # Ne pas exposer trop de détails en prod si tu préfères
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {e}")

    filename = _safe_filename(req.title)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/generate-pdf-json")
def generate_pdf_json(req: PdfRequest, x_api_key: Optional[str] = Header(default=None)):
    """
    Retour JSON { filename, pdf_base64 }
    """
    _check_key(x_api_key)
    try:
        html_str = _render_html(req)
        pdf_bytes = _generate_pdf_bytes(html_str)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {e}")

    filename = _safe_filename(req.title)
    b64 = base64.b64encode(pdf_bytes).decode("ascii")
    return JSONResponse({"filename": filename, "pdf_base64": b64})


@app.post("/generate-pdf-url")
def generate_pdf_url(
    req: PdfRequest,
    background_tasks: BackgroundTasks,
    x_api_key: Optional[str] = Header(default=None),
):
    """
    Génère un PDF, le stocke temporairement, renvoie { pdf_url, expires_at }.
    Le PDF est disponible via GET /download/{token}.
    """
    _check_key(x_api_key)

    # nettoyage best-effort avant génération
    _cleanup_expired()
    _ensure_store()

    try:
        html_str = _render_html(req)
        pdf_bytes = _generate_pdf_bytes(html_str)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {e}")

    token = uuid.uuid4().hex  # non devinable
    file_path = Path(PDF_STORE_DIR) / f"{token}.pdf"
    try:
        file_path.write_bytes(pdf_bytes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot write PDF: {e}")

    # Optionnel : un cleanup après réponse (best-effort)
    background_tasks.add_task(_cleanup_expired)

    expires_at = int(time.time() + PDF_TTL_SECONDS)
    pdf_url = f"{PUBLIC_BASE_URL}/download/{token}"

    return {"pdf_url": pdf_url, "expires_at": expires_at}


@app.get("/download/{token}")
def download_pdf(token: str):
    """
    Télécharge le PDF généré. Peut être supprimé après 1er download si DELETE_AFTER_FIRST_DOWNLOAD=true.
    """
    token = (token or "").strip()
    # validation simple
    if not token.isalnum() or len(token) < 16:
        raise HTTPException(status_code=400, detail="Invalid token")

    file_path = Path(PDF_STORE_DIR) / f"{token}.pdf"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found or expired")

    try:
        pdf_bytes = file_path.read_bytes()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot read PDF: {e}")

    if DELETE_AFTER_FIRST_DOWNLOAD:
        try:
            file_path.unlink(missing_ok=True)
        except Exception:
            pass

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="audit.pdf"',
            "Cache-Control": "no-store",
        },
    )
