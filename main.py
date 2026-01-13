from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
import requests
import re
import json
import time
from typing import Any, Dict, List, Optional, Tuple

app = FastAPI(title="YouTube Scraper (Videos, Lives, Shorts)")

YOUTUBE_ROOT = "https://www.youtube.com"

# --- Modelo de Resposta Atualizado (Apenas o que você pediu) ---
class VideoItem(BaseModel):
    url: str
    title: str
    description: str
    thumbnail: str

class ChannelResponse(BaseModel):
    channel: str
    total_videos: int
    total_lives: int
    total_shorts: int
    videos: List[VideoItem]
    lives: List[VideoItem]
    shorts: List[VideoItem]

# --- Funções Auxiliares ---

def _extract_innertube(html: str) -> Tuple[str, str]:
    api_key_m = re.search(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"', html)
    ver_m = re.search(r'"INNERTUBE_CLIENT_VERSION"\s*:\s*"([^"]+)"', html)
    if not api_key_m or not ver_m:
        api_key_m = re.search(r'INNERTUBE_API_KEY":"([^"]+)"', html)
        ver_m = re.search(r'INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
        if not api_key_m or not ver_m:
            raise RuntimeError("Não foi possível extrair chaves da API.")
    return api_key_m.group(1), ver_m.group(1)

def _extract_ytinitialdata(html: str) -> Dict[str, Any]:
    m = re.search(r"var\s+ytInitialData\s*=\s*(\{.*?\});", html, flags=re.DOTALL)
    if not m:
        m = re.search(r"ytInitialData\s*=\s*(\{.*?\});", html, flags=re.DOTALL)
    if not m:
        raise RuntimeError("Não foi possível extrair dados iniciais.")
    return json.loads(m.group(1))

def _walk(obj: Any):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(obj, list):
        for it in obj:
            yield from _walk(it)

def _pick_text(t: Any) -> str:
    if not isinstance(t, dict): return ""
    if "simpleText" in t: return t["simpleText"]
    if "runs" in t: return "".join(r.get("text", "") for r in t["runs"])
    return ""

def _parse_renderer(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Parser robusto para Vídeos, Lives e Shorts
    """
    video_id = None
    
    # 1. Tenta pegar o ID (pode estar na raiz ou dentro do endpoint)
    if "videoId" in data:
        video_id = data["videoId"]
    elif "navigationEndpoint" in data:
        # Shorts costumam esconder o ID aqui
        nav = data["navigationEndpoint"]
        if "watchEndpoint" in nav and "videoId" in nav["watchEndpoint"]:
            video_id = nav["watchEndpoint"]["videoId"]
        elif "reelWatchEndpoint" in nav and "videoId" in nav["reelWatchEndpoint"]:
            video_id = nav["reelWatchEndpoint"]["videoId"]
    
    if not video_id:
        return None

    # 2. Título (Vídeos usam 'title', Shorts usam 'headline')
    title = _pick_text(data.get("title", {})) or _pick_text(data.get("headline", {}))
    
    # 3. Descrição (Snippet)
    desc = ""
    if "descriptionSnippet" in data:
        desc = _pick_text(data["descriptionSnippet"])
    elif data.get("detailedMetadataSnippets"):
        desc = _pick_text(data["detailedMetadataSnippets"][0].get("snippetText", {}))

    # 4. Thumbnail
    thumb = ""
    if "thumbnail" in data:
        thumbs = data["thumbnail"].get("thumbnails", [])
        if thumbs: thumb = thumbs[-1].get("url", "")
    elif "thumbnails" in data: # Alguns shorts usam estrutura diferente
        thumbs = data["thumbnails"]
        if thumbs: thumb = thumbs[0].get("url", "")

    return {
        "id_interno": video_id, # Usado apenas para deduplicar, não sai no JSON final
        "url": f"{YOUTUBE_ROOT}/watch?v={video_id}", # Ou /shorts/, mas watch funciona pra tudo
        "title": title,
        "description": desc,
        "thumbnail": thumb
    }

def _extract_items_and_continuation(data: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    items = []
    continuation = None
    
    # Procura por renderers conhecidos
    # reelItemRenderer = Shorts
    # videoRenderer = Videos/Lives passadas
    # gridVideoRenderer = Videos em layout de grade
    target_keys = ("gridVideoRenderer", "videoRenderer", "reelItemRenderer", "shortsLockupViewModel")
    
    for k, v in _walk(data):
        if k in target_keys and isinstance(v, dict):
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
    try:
        resp = session.post(url, headers=headers, json=payload, timeout=30)
        if resp.status_code >= 400: return {}
        return resp.json()
    except:
        return {}

def scrape_specific_tab(session, base_url: str, tab_suffix: str, max_items: Optional[int]) -> List[Dict[str, Any]]:
    target_url = base_url.rstrip("/") + tab_suffix
    try:
        r = session.get(target_url, timeout=20)
        if r.status_code != 200: return []
        
        html = r.text
        try:
            api_key, client_version = _extract_innertube(html)
            initial = _extract_ytinitialdata(html)
        except:
            return []

        items = []
        seen_ids = set()
        
        page_items, continuation = _extract_items_and_continuation(initial)
        for i in page_items:
            if i["id_interno"] not in seen_ids:
                seen_ids.add(i["id_interno"])
                # Remove o ID interno antes de salvar na lista final
                i_clean = {k: v for k, v in i.items() if k != "id_interno"}
                items.append(i_clean)

        while continuation:
            if max_items and len(items) >= max_items: break
            time.sleep(0.1)
            
            data = _browse_continuation(session, api_key, client_version, continuation, target_url)
            page_items, new_cont = _extract_items_and_continuation(data)
            
            if not page_items: break

            for i in page_items:
                if i["id_interno"] not in seen_ids:
                    seen_ids.add(i["id_interno"])
                    i_clean = {k: v for k, v in i.items() if k != "id_interno"}
                    items.append(i_clean)
                    if max_items and len(items) >= max_items: break
            
            if not new_cont or new_cont == continuation: break
            continuation = new_cont

        if max_items:
            items = items[:max_items]
            
        return items

    except Exception:
        return []

# --- Rota Principal ---

@app.get("/")
def home():
    return {"message": "API YouTube Limpa (Videos, Lives, Shorts). Use /scrape"}

@app.get("/scrape", response_model=ChannelResponse)
def scrape_all(
    channel_url: str = Query(..., description="URL do canal"),
    max_videos: Optional[int] = Query(None, description="Limite vídeos"),
    max_lives: Optional[int] = Query(None, description="Limite lives"),
    max_shorts: Optional[int] = Query(None, description="Limite shorts"),
):
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept-Language": "pt-BR"
    })
    
    base_url = channel_url.split("?")[0].rstrip("/")

    videos = scrape_specific_tab(session, base_url, "/videos", max_videos)
    lives = scrape_specific_tab(session, base_url, "/streams", max_lives)
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
