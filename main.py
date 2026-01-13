from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
import requests
import re
import json
import time
from typing import Any, Dict, List, Optional, Tuple

app = FastAPI(title="YouTube Channel Scraper API")

YOUTUBE_ROOT = "https://www.youtube.com"

# --- Modelos de Resposta ---
class VideoItem(BaseModel):
    url: str
    title: str
    description: str
    thumbnail: str

class ChannelResponse(BaseModel):
    channel: str
    count: int
    videos: List[VideoItem]

# --- Funções Auxiliares (Do seu script original) ---

def _extract_innertube(html: str) -> Tuple[str, str]:
    api_key_m = re.search(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"', html)
    ver_m = re.search(r'"INNERTUBE_CLIENT_VERSION"\s*:\s*"([^"]+)"', html)
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

def _parse_video_renderer(vr: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    vid = vr.get("videoId")
    if not vid: return None
    title = _pick_text(vr.get("title", {}))
    
    desc = ""
    if "descriptionSnippet" in vr:
        desc = _pick_text(vr["descriptionSnippet"])
    elif vr.get("detailedMetadataSnippets"):
        desc = _pick_text(vr["detailedMetadataSnippets"][0].get("snippetText", {}))

    thumb = vr.get("thumbnail", {}).get("thumbnails", [])[-1].get("url", "") if vr.get("thumbnail", {}).get("thumbnails") else ""

    return {
        "videoId": vid,
        "url": f"{YOUTUBE_ROOT}/watch?v={vid}",
        "title": title,
        "description": desc,
        "thumbnail": thumb,
    }

def _extract_videos_and_continuation(data: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    videos = []
    continuation = None
    
    for k, v in _walk(data):
        if k in ("gridVideoRenderer", "videoRenderer"):
            parsed = _parse_video_renderer(v)
            if parsed: videos.append(parsed)
            
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
                
    return videos, continuation

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

def _fetch_full_description(session, video_url):
    try:
        r = session.get(video_url, timeout=10) # Timeout menor para não travar a API
        m = re.search(r"var\s+ytInitialPlayerResponse\s*=\s*(\{.*?\});", html := r.text, flags=re.DOTALL)
        if m:
            data = json.loads(m.group(1))
            return data.get("videoDetails", {}).get("shortDescription", "")
    except:
        pass
    return ""

# --- Rotas da API ---

@app.get("/")
def home():
    return {"message": "YouTube Channel Scraper API is running. Use /scrape?channel_url=... to get videos."}

@app.get("/scrape", response_model=ChannelResponse)
def scrape_channel(
    channel_url: str = Query(..., description="URL completa do canal (ex: https://www.youtube.com/@NicoChat)"),
    max_videos: Optional[int] = Query(10, description="Limite de vídeos para retornar"),
    full_description: bool = Query(False, description="Se True, entra em cada vídeo para pegar descrição completa (Mais lento!)")
):
    """
    Coleta vídeos de um canal do YouTube.
    """
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0", "Accept-Language": "pt-BR"})
        videos_url = channel_url.rstrip("/") + "/videos"

        # 1. Carregar página inicial do canal
        r = session.get(videos_url, timeout=30)
        if r.status_code != 200:
            raise HTTPException(status_code=400, detail="Não foi possível acessar o canal. Verifique a URL.")
        
        html = r.text
        try:
            api_key, client_version = _extract_innertube(html)
            initial = _extract_ytinitialdata(html)
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e))

        videos = []
        seen = set()
        
        page_videos, continuation = _extract_videos_and_continuation(initial)
        for v in page_videos:
            if v["videoId"] not in seen:
                seen.add(v["videoId"])
                videos.append(v)

        # 2. Loop de paginação
        while continuation and (not max_videos or len(videos) < max_videos):
            # Limite de segurança para não rodar infinito na API
            if len(videos) >= 500: break 
            
            data = _browse_continuation(session, api_key, client_version, continuation, videos_url)
            page_videos, new_cont = _extract_videos_and_continuation(data)
            
            for v in page_videos:
                if v["videoId"] not in seen:
                    seen.add(v["videoId"])
                    videos.append(v)
                    if max_videos and len(videos) >= max_videos: break
            
            if not new_cont or new_cont == continuation: break
            continuation = new_cont

        # Cortar excesso
        if max_videos:
            videos = videos[:max_videos]

        # 3. Descrição completa (Opcional - Pode deixar a API lenta)
        if full_description:
            for v in videos:
                full_desc = _fetch_full_description(session, v["url"])
                if full_desc: v["description"] = full_desc

        return {
            "channel": channel_url,
            "count": len(videos),
            "videos": videos
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")