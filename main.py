from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
import requests
import re
import json
import time
from typing import Any, Dict, List, Optional, Tuple

app = FastAPI(title="YouTube Multi-Tab Scraper (Videos, Lives, Shorts)")

YOUTUBE_ROOT = "https://www.youtube.com"

# --- Modelos de Resposta ---
class VideoItem(BaseModel):
    id: str
    url: str
    title: str
    description: str
    thumbnail: str
    view_count: str = "" # Opcional, útil se disponível

class ChannelResponse(BaseModel):
    channel: str
    total_videos: int
    total_lives: int
    total_shorts: int
    videos: List[VideoItem]
    lives: List[VideoItem]
    shorts: List[VideoItem]

# --- Funções Auxiliares de Extração ---

def _extract_innertube(html: str) -> Tuple[str, str]:
    api_key_m = re.search(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"', html)
    ver_m = re.search(r'"INNERTUBE_CLIENT_VERSION"\s*:\s*"([^"]+)"', html)
    if not api_key_m or not ver_m:
        # Tenta fallback para outro padrão comum
        api_key_m = re.search(r'INNERTUBE_API_KEY":"([^"]+)"', html)
        ver_m = re.search(r'INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
        if not api_key_m or not ver_m:
            raise RuntimeError("Não foi possível extrair chaves da API do YouTube.")
    return api_key_m.group(1), ver_m.group(1)

def _extract_ytinitialdata(html: str) -> Dict[str, Any]:
    m = re.search(r"var\s+ytInitialData\s*=\s*(\{.*?\});", html, flags=re.DOTALL)
    if not m:
        m = re.search(r"ytInitialData\s*=\s*(\{.*?\});", html, flags=re.DOTALL)
    if not m:
        raise RuntimeError("Não foi possível extrair dados iniciais do HTML.")
    return json.loads(m.group(1))

def _walk(obj: Any):
    """Percorre recursivamente o JSON complexo do YouTube"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(obj, list):
        for it in obj:
            yield from _walk(it)

def _pick_text(t: Any) -> str:
    """Extrai texto de estruturas complexas do YouTube"""
    if not isinstance(t, dict): return ""
    if "simpleText" in t: return t["simpleText"]
    if "runs" in t: return "".join(r.get("text", "") for r in t["runs"])
    return ""

def _parse_renderer(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Analisa tanto Vídeos/Lives (videoRenderer) quanto Shorts (reelItemRenderer)
    """
    # 1. Tenta formato padrão (Vídeos e Lives)
    if "videoId" in data and ("title" in data or "headline" in data):
        vid = data["videoId"]
        title = _pick_text(data.get("title", {})) or _pick_text(data.get("headline", {}))
        
        # Descrição
        desc = ""
        if "descriptionSnippet" in data:
            desc = _pick_text(data["descriptionSnippet"])
        elif data.get("detailedMetadataSnippets"):
            desc = _pick_text(data["detailedMetadataSnippets"][0].get("snippetText", {}))

        # Thumbnail
        thumb = ""
        if "thumbnail" in data:
            thumbs = data["thumbnail"].get("thumbnails", [])
            if thumbs: thumb = thumbs[-1].get("url", "")
        
        # View Count (Visualizações)
        views = _pick_text(data.get("viewCountText", {})) or _pick_text(data.get("shortViewCountText", {}))

        return {
            "id": vid,
            "url": f"{YOUTUBE_ROOT}/watch?v={vid}",
            "title": title,
            "description": desc,
            "thumbnail": thumb,
            "view_count": views
        }
    
    return None

def _extract_items_and_continuation(data: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    items = []
    continuation = None
    
    # Procura por Renderers de Vídeo, Grid de Vídeo ou Shorts (reelItem)
    for k, v in _walk(data):
        if k in ("gridVideoRenderer", "videoRenderer", "reelItemRenderer") and isinstance(v, dict):
            parsed = _parse_renderer(v)
            if parsed: items.append(parsed)
            
    # Procura token de continuação
    for k, v in _walk(data):
        if k == "continuationItemRenderer":
            token = v.get("continuationEndpoint", {}).get("continuationCommand", {}).get("token")
            if token:
                continuation = token
                break
    
    if not continuation:
        for k, v in _walk(data):
            if k == "continuationCommand" and v.get("token"):
                continuation = v["token"]
                break
                
    return items, continuation

def _browse_continuation(session, api_key, client_version, continuation, referer):
    url = f"{YOUTUBE_ROOT}/youtubei/v1/browse?key={api_key}"
    headers = {
        "Content-Type": "application/json",
        "Origin": YOUTUBE_ROOT,
        "Referer": referer,
        "X-Youtube-Client-Name": "1",
        "X-Youtube-Client-Version": client_version,
        "User-Agent": "Mozilla/5.0"
    }
    payload = {
        "context": {
            "client": {
                "hl": "pt-BR", "gl": "BR", "clientName": "WEB", "clientVersion": client_version
            }
        },
        "continuation": continuation,
    }
    resp = session.post(url, headers=headers, json=payload, timeout=30)
    if resp.status_code >= 400: return {}
    return resp.json()

# --- Função Core de Raspagem por Aba ---

def scrape_specific_tab(session, base_url: str, tab_suffix: str, max_items: Optional[int]) -> List[Dict[str, Any]]:
    """
    Acessa uma aba específica (videos, streams, shorts) e baixa tudo.
    """
    target_url = base_url.rstrip("/") + tab_suffix
    print(f"Scraping tab: {target_url}")
    
    try:
        r = session.get(target_url, timeout=20)
        # Se a aba não existir (ex: canal não tem lives), o YouTube costuma redirecionar ou dar 200 na Home.
        # O parser vai simplesmente não encontrar nada, o que é o comportamento correto (retorna lista vazia).
        if r.status_code != 200:
            return []
        
        html = r.text
        try:
            api_key, client_version = _extract_innertube(html)
            initial = _extract_ytinitialdata(html)
        except:
            # Se falhar em extrair dados iniciais, assume que a aba está vazia ou inacessível
            return []

        items = []
        seen = set()
        
        page_items, continuation = _extract_items_and_continuation(initial)
        for i in page_items:
            if i["id"] not in seen:
                seen.add(i["id"])
                items.append(i)

        # Loop de Paginação
        while continuation:
            if max_items and len(items) >= max_items: break
            
            time.sleep(0.1) # Delay anti-bloqueio
            
            data = _browse_continuation(session, api_key, client_version, continuation, target_url)
            page_items, new_cont = _extract_items_and_continuation(data)
            
            if not page_items: break

            for i in page_items:
                if i["id"] not in seen:
                    seen.add(i["id"])
                    items.append(i)
                    if max_items and len(items) >= max_items: break
            
            if not new_cont or new_cont == continuation: break
            continuation = new_cont

        if max_items:
            items = items[:max_items]
            
        return items

    except Exception as e:
        print(f"Erro ao processar aba {tab_suffix}: {e}")
        return []

# --- Rota Principal ---

@app.get("/")
def home():
    return {"message": "API YouTube Completa (Videos, Lives, Shorts). Use /scrape"}

@app.get("/scrape", response_model=ChannelResponse)
def scrape_all(
    channel_url: str = Query(..., description="URL do canal"),
    max_videos: Optional[int] = Query(None, description="Limite para vídeos normais"),
    max_lives: Optional[int] = Query(None, description="Limite para lives"),
    max_shorts: Optional[int] = Query(None, description="Limite para shorts"),
):
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0", "Accept-Language": "pt-BR"})
    
    # Garante URL limpa
    base_url = channel_url.split("?")[0].rstrip("/")

    # 1. Busca Vídeos (/videos)
    videos = scrape_specific_tab(session, base_url, "/videos", max_videos)
    
    # 2. Busca Lives (/streams)
    lives = scrape_specific_tab(session, base_url, "/streams", max_lives)
    
    # 3. Busca Shorts (/shorts)
    shorts = scrape_specific_tab(session, base_url, "/shorts", max_shorts)

    return {
        "channel": base_url,
        "total_videos": len(videos),
        "total_lives": len(lives),
        "total_shorts": len(shorts),
        "videos": videos,
        "lives": lives,
        "shorts": shorts
    }
