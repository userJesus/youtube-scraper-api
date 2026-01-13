from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
import requests
import re
import json
import time
from typing import Any, Dict, List, Optional, Tuple

app = FastAPI(title="YouTube Scraper (Videos, Lives, Shorts)")

YOUTUBE_ROOT = "https://www.youtube.com"

# --- Modelo de Resposta ---
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
    """Percorre recursivamente o JSON"""
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
    if "content" in t: return t["content"] # Usado em alguns ViewModels novos
    if "runs" in t: return "".join(r.get("text", "") for r in t["runs"])
    return ""

def _parse_item(data: Dict[str, Any], is_short: bool = False) -> Optional[Dict[str, Any]]:
    """
    Parser universal que detecta o tipo de item e extrai os dados.
    Suporta: videoRenderer, reelItemRenderer, shortsLockupViewModel
    """
    video_id = None
    title = ""
    thumb = ""
    desc = ""

    # --- ESTRATÉGIA 1: Format Antigo (Renderers) ---
    if "videoId" in data:
        video_id = data["videoId"]
        title = _pick_text(data.get("title", {})) or _pick_text(data.get("headline", {}))
        
        if "thumbnail" in data:
            thumbs = data["thumbnail"].get("thumbnails", [])
            if thumbs: thumb = thumbs[-1].get("url", "")
            
        if "descriptionSnippet" in data:
            desc = _pick_text(data["descriptionSnippet"])

    # --- ESTRATÉGIA 2: Format Novo (ViewModels / Shorts) ---
    # O YouTube agora usa 'shortsLockupViewModel' onde o ID fica dentro de um evento de click
    elif "onTap" in data:
        # Tenta achar o ID dentro do comando de navegação
        cmd = data["onTap"]
        for k, v in _walk(cmd):
            if k in ("reelWatchEndpoint", "watchEndpoint") and "videoId" in v:
                video_id = v["videoId"]
                break
        
        # Se achou ID, tenta achar metadados no mesmo nível
        if video_id:
            # Título geralmente está em overlayMetadata -> primaryText
            if "overlayMetadata" in data:
                overlay = data["overlayMetadata"]
                title = _pick_text(overlay.get("primaryText", {}))
            
            # Thumbnail geralmente está direto no objeto ou em 'thumbnail'
            if "thumbnail" in data:
                 thumbs = data["thumbnail"].get("sources", [])
                 if thumbs: thumb = thumbs[-1].get("url", "")

    # --- ESTRATÉGIA 3: Navigation Endpoint (Reels antigos) ---
    elif "navigationEndpoint" in data:
         nav = data["navigationEndpoint"]
         if "reelWatchEndpoint" in nav:
             video_id = nav["reelWatchEndpoint"].get("videoId")
             title = _pick_text(data.get("headline", {}))
             if "thumbnail" in data:
                thumbs = data.get("thumbnail", {}).get("thumbnails", [])
                if thumbs: thumb = thumbs[0].get("url", "")

    if not video_id:
        return None

    # Formatação da URL
    if is_short:
        final_url = f"{YOUTUBE_ROOT}/shorts/{video_id}"
    else:
        final_url = f"{YOUTUBE_ROOT}/watch?v={video_id}"

    return {
        "id_interno": video_id,
        "url": final_url,
        "title": title,
        "description": desc, # Shorts geralmente não têm snippet de descrição na lista
        "thumbnail": thumb
    }

def _extract_from_initial_data(data: Dict[str, Any], is_short_tab: bool) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    items = []
    continuation = None
    
    # Chaves que indicam um vídeo/short válido
    target_keys = ("gridVideoRenderer", "videoRenderer", "reelItemRenderer", "shortsLockupViewModel")

    for k, v in _walk(data):
        if k in target_keys and isinstance(v, dict):
            # Forçamos is_short=True se encontrarmos estruturas típicas de Shorts ou se estivermos na aba Shorts
            is_item_short = is_short_tab or k in ("reelItemRenderer", "shortsLockupViewModel")
            parsed = _parse_item(v, is_short=is_item_short)
            if parsed: items.append(parsed)

    # Busca token de continuação
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

def _browse_req(session, api_key, client_version, continuation, referer):
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
        r = session.post(url, headers=headers, json=payload, timeout=30)
        if r.status_code >= 400: return {}
        return r.json()
    except:
        return {}

def scrape_tab(session, base_url: str, tab: str, max_items: Optional[int]) -> List[Dict[str, Any]]:
    # Define a URL da aba
    if tab == "shorts":
        target_url = base_url.rstrip("/") + "/shorts"
    elif tab == "lives":
        target_url = base_url.rstrip("/") + "/streams"
    else:
        target_url = base_url.rstrip("/") + "/videos"

    print(f"Acessando: {target_url}")
    
    try:
        r = session.get(target_url, timeout=20)
        # Se redirecionar ou não for 200, assume vazio
        if r.url != target_url and tab != "videos": # Às vezes /videos redireciona para Home se vazio, ok ignorar
             pass 
        
        html = r.text
        try:
            api_key, client_version = _extract_innertube(html)
            initial = _extract_ytinitialdata(html)
        except:
            return []

        items = []
        seen_ids = set()
        
        # Extração inicial
        page_items, continuation = _extract_from_initial_data(initial, is_short_tab=(tab=="shorts"))
        
        for i in page_items:
            if i["id_interno"] not in seen_ids:
                seen_ids.add(i["id_interno"])
                items.append({k:v for k,v in i.items() if k != "id_interno"})

        # Paginação
        while continuation:
            if max_items and len(items) >= max_items: break
            time.sleep(0.1)
            
            data = _browse_req(session, api_key, client_version, continuation, target_url)
            page_items, new_cont = _extract_from_initial_data(data, is_short_tab=(tab=="shorts"))
            
            if not page_items: break

            for i in page_items:
                if i["id_interno"] not in seen_ids:
                    seen_ids.add(i["id_interno"])
                    items.append({k:v for k,v in i.items() if k != "id_interno"})
                    if max_items and len(items) >= max_items: break
            
            if not new_cont or new_cont == continuation: break
            continuation = new_cont

        if max_items:
            items = items[:max_items]
        return items

    except Exception as e:
        print(f"Erro em {tab}: {e}")
        return []

# --- Rota API ---

@app.get("/")
def home():
    return {"message": "API YouTube Scraper V2. Use /scrape"}

@app.get("/scrape", response_model=ChannelResponse)
def scrape_channel(
    channel_url: str = Query(..., description="URL do canal"),
    max_videos: Optional[int] = Query(None),
    max_lives: Optional[int] = Query(None),
    max_shorts: Optional[int] = Query(None),
):
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept-Language": "pt-BR"
    })
    
    # Limpa URL
    base_url = channel_url.split("?")[0].rstrip("/")
    
    # Coleta paralela (sequencial no código, mas separada)
    videos = scrape_tab(session, base_url, "videos", max_videos)
    lives = scrape_tab(session, base_url, "lives", max_lives)
    shorts = scrape_tab(session, base_url, "shorts", max_shorts)

    return {
        "channel": base_url,
        "total_videos": len(videos),
        "total_lives": len(lives),
        "total_shorts": len(shorts),
        "videos": videos,
        "lives": lives,
        "shorts": shorts
    }
