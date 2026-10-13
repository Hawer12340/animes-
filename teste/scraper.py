import json
from html import unescape
import re
import sys
import io
import os
import msvcrt
import time
import hashlib
import asyncio
import requests
from functools import wraps
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunsplit, parse_qs, unquote

try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)


def log(msg=''):
    print(msg, flush=True)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}
# Resolve the database next to this script so launching it from an IDE or a
# different working directory does not silently read/write another file.
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "animes.json")


def _serialize_database_access(func):
    """Serializa las operaciones de lectura/modificación/escritura entre procesos."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        lock_path = OUTPUT_FILE + ".lock"
        with open(lock_path, "a+b") as lock_file:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"0")
                lock_file.flush()
            while True:
                lock_file.seek(0)
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
            try:
                return func(*args, **kwargs)
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    return wrapped


def _write_database_file(records):
    """Troca o JSON de uma vez para leitores nunca pegarem arquivo pela metade."""
    temp_path = f"{OUTPUT_FILE}.{os.getpid()}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, OUTPUT_FILE)


# ═══════════════════════════════════════════════════════════
# JSON ÚNICO + ANTI-DUPLICATA + SEPARAÇÃO DE VERSÕES
# ═══════════════════════════════════════════════════════════

VERSION_ALIASES = {
    "dub": "dublado",
    "dubbed": "dublado",
    "dublado": "dublado",
    "leg": "legendado",
    "sub": "legendado",
    "legendado": "legendado",
    "legendado": "legendado",
}


def normalize_version(value):
    if not value:
        return ""
    text = str(value).strip().lower()
    text = re.sub(r"[^a-záàâãéêíóôõúç0-9]+", "", text)
    return VERSION_ALIASES.get(text, text)


def normalize_title(title):
    """Normaliza o título sem misturar as versões dublado/legendado."""
    title = str(title or "Desconhecido")
    title = re.sub(r"\s+", " ", title).strip()
    title = re.sub(
        r"\s*(?:\[|\(|\-|\||/)?\s*(?:dublado|legendado|dub|dubbed|leg|sub)\s*(?:\]|\))?\s*$",
        "",
        title,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", title).strip() or "Desconhecido"


def guess_version_from_text(*parts):
    text = " ".join(str(p or "") for p in parts).lower()
    if re.search(r"\b(?:dublado|dubbed|dub)\b", text):
        return "dublado"
    if re.search(r"\b(?:legendado|leg|sub)\b", text):
        return "legendado"
    if re.search(r"(?:/|_|-|\.)dub(?:/|_|-|\.|$)", text):
        return "dublado"
    if re.search(r"(?:/|_|-|\.)leg(?:/|_|-|\.|$)", text):
        return "legendado"
    return ""


def record_version(record):
    """Obtém a versão de um registro já salvo."""
    direct = normalize_version(record.get("version", ""))
    if direct in ("dublado", "legendado"):
        return direct

    versions = set()
    for ep in record.get("episodes", []) or []:
        v = normalize_version(ep.get("version", ""))
        if v in ("dublado", "legendado"):
            versions.add(v)
        else:
            v = guess_version_from_text(ep.get("name"), ep.get("url"))
            if v:
                versions.add(v)

    if len(versions) == 1:
        return next(iter(versions))
    if len(versions) > 1:
        return "misto"

    return guess_version_from_text(record.get("title"), record.get("source_url")) or "indefinido"


def get_anime_id(title, site, source_url="", version=""):
    """ID estável por título + versão, para reunir players de sites diferentes."""
    canonical_title = normalize_title(title).lower()
    canonical_version = normalize_version(version) or "indefinido"
    base = f"{canonical_title}|{canonical_version}"
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:12]


def _unique_values(values):
    result = []
    seen = set()
    for value in values:
        if value in (None, ""):
            continue
        key = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _unique_urls(values):
    """Deduplica players/fontes pelo URL, mantendo o primeiro metadado encontrado."""
    result = []
    seen = set()
    for value in values:
        if isinstance(value, dict):
            url = str(value.get("url") or "").strip()
            item = dict(value)
            item["url"] = url
        else:
            url = str(value or "").strip()
            item = url
        if not url or url in seen:
            continue
        seen.add(url)
        result.append(item)
    return result


def _merge_episode(old, new, version):
    """Mescla um episódio repetido, preservando todas as fontes encontradas."""
    merged = dict(old or {})
    new = dict(new or {})
    merged["season"] = int(new.get("season", merged.get("season", 1)) or 1)
    merged["number"] = int(new.get("number", merged.get("number", 0)) or 0)
    merged["version"] = normalize_version(version) or normalize_version(new.get("version")) or normalize_version(old.get("version"))

    for key in ("name", "error"):
        if not merged.get(key) and new.get(key):
            merged[key] = new[key]
    # O link da página do episódio precisa acompanhar o item recém-encontrado;
    # bases antigas podem conter um episode_url repetido de outro título.
    if new.get("episode_url"):
        merged["episode_url"] = new["episode_url"]

    # Se o JSON antigo tinha apenas URL de página/player e agora encontramos
    # a mídia real (.m3u8/.mp4), troca a URL principal pela mídia real.
    old_url = merged.get("url", "") or ""
    new_url = new.get("url", "") or ""
    def is_direct_media(u):
        low = str(u).lower()
        return bool(u) and "videohls.php" not in low and (
            re.search(r"\.(?:m3u8|mp4)(?:\?|$)", low) or
            "googlevideo.com" in low
        )
    def is_page_asset(u):
        return bool(u) and bool(re.search(r"\.(?:js|css|jpe?g|png|gif|webp|svg)(?:[?#]|$)", str(u), re.I))
    def is_m3_player(u):
        return bool(u) and urlparse(str(u)).path.lower().startswith('/m3/')
    if not old_url and new_url:
        merged["url"] = new_url
    elif is_page_asset(old_url) and new_url and not is_page_asset(new_url):
        merged["url"] = new_url
    elif is_m3_player(new_url) and not is_m3_player(old_url):
        merged["url"] = new_url
    elif is_direct_media(new_url) and not is_direct_media(old_url):
        merged["url"] = new_url

    old_sources = merged.get("sources", []) or []
    new_sources = new.get("sources", []) or []
    if old_sources or new_sources:
        merged["sources"] = _unique_urls(old_sources + new_sources)

    old_players = merged.get("players", []) or []
    new_players = new.get("players", []) or []
    if old_players or new_players:
        merged["players"] = _unique_urls(old_players + new_players)
        merged["player_count"] = len(merged["players"])

    for key in ("test", "ok"):
        if merged.get(key) is None and new.get(key) is not None:
            merged[key] = new[key]

    if merged.get("sources") and merged.get("players"):
        player_urls = {str(p.get("url") if isinstance(p, dict) else p).strip() for p in merged["players"]}
        merged["sources"] = [
            src for src in merged["sources"]
            if str(src.get("url") if isinstance(src, dict) else src).strip() not in player_urls
        ]
    if merged.get("sources"):
        merged["source_count"] = len(merged["sources"])
    # Links repetidos entre os dois formatos contam como um único servidor.
    return merged


def _merge_record(base, incoming, version):
    """Mescla dois registros do mesmo anime/site/versão."""
    record = dict(base or {})
    incoming = dict(incoming or {})
    base_site = record.get("site", "")
    incoming_site = incoming.get("site", "")
    record["site"] = base_site or incoming_site
    record["sites"] = _unique_values(
        (record.get("sites", []) or []) + ([base_site] if base_site else []) +
        (incoming.get("sites", []) or []) + ([incoming_site] if incoming_site else [])
    )
    record["title"] = normalize_title(incoming.get("title") or record.get("title"))
    record["version"] = normalize_version(version) or record.get("version") or "indefinido"

    for field in ("image", "synopsis", "genres", "info", "seasons_count", "source_url"):
        value = incoming.get(field)
        if value not in (None, "", [], {}):
            if field == "source_url" and record.get(field):
                # Não perde a primeira URL, mas guarda alternativas.
                urls = _unique_values([record.get(field), value] + record.get("source_urls", []))
                record["source_url"] = urls[0]
                record["source_urls"] = urls
            else:
                record[field] = value

    episode_map = {}
    for ep in (record.get("episodes", []) or []) + (incoming.get("episodes", []) or []):
        ep = dict(ep)
        ep["version"] = normalize_version(ep.get("version")) or record["version"]
        key = (int(ep.get("season", 1) or 1), int(ep.get("number", 0) or 0), ep["version"])
        if key[1] <= 0:
            continue
        episode_map[key] = _merge_episode(episode_map.get(key), ep, record["version"])

    record["episodes"] = sorted(
        episode_map.values(),
        key=lambda ep: (int(ep.get("season", 1)), int(ep.get("number", 0)), ep.get("version", "")),
    )
    record["total_episodes"] = len(record["episodes"])
    record["id"] = get_anime_id(record["title"], record["site"], record.get("source_url", ""), record["version"])
    record["extracted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return record


def _repair_database_records(records):
    """Conserta animes.json antigo, separando registros mistos em dublado/legendado."""
    grouped = {}
    changed = False

    for original in records or []:
        if not isinstance(original, dict):
            changed = True
            continue

        site = original.get("site", "")
        title = normalize_title(original.get("title") or original.get("name") or "Desconhecido")
        default_version = normalize_version(original.get("version", ""))
        if default_version not in ("dublado", "legendado"):
            default_version = ""

        episodes = original.get("episodes", []) or []
        by_version = {}
        for ep in episodes:
            ep = dict(ep)
            version = normalize_version(ep.get("version", ""))
            if version not in ("dublado", "legendado"):
                version = guess_version_from_text(ep.get("name"), ep.get("url")) or default_version
            if version not in ("dublado", "legendado"):
                # Não inventa uma versão para dados realmente sem identificação.
                version = "indefinido"
            ep["version"] = version
            by_version.setdefault(version, []).append(ep)

        if not by_version:
            by_version[default_version or "indefinido"] = []

        if len(by_version) > 1 or normalize_title(original.get("title", "")) != str(original.get("title", "")).strip():
            changed = True

        for version, eps in by_version.items():
            key = (title.lower(), version)
            incoming = dict(original)
            incoming["title"] = title
            incoming["version"] = version
            incoming["episodes"] = eps
            incoming["total_episodes"] = len(eps)
            incoming["id"] = get_anime_id(title, site, incoming.get("source_url", ""), version)
            if key in grouped:
                grouped[key] = _merge_record(grouped[key], incoming, version)
                changed = True
            else:
                cleaned = _merge_record({}, incoming, version)
                grouped[key] = cleaned

    repaired = list(grouped.values())
    repaired.sort(key=lambda a: (a.get("site", ""), a.get("title", "").lower(), a.get("version", "")))
    if repaired != records:
        changed = True
    return repaired, changed


def load_animes():
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, list):
            raw = []
    except (FileNotFoundError, json.JSONDecodeError):
        return []

    repaired, changed = _repair_database_records(raw)
    if changed:
        _write_database_file(repaired)
        print(f"\n  🧹 animes.json corrigido automaticamente: {len(raw)} -> {len(repaired)} registros.", flush=True)
    return repaired


def save_animes(animes):
    repaired, _ = _repair_database_records(animes)
    _write_database_file(repaired)
    print(f"  [JSON] Salvo: {OUTPUT_FILE} ({len(repaired)} registros)", flush=True)


def is_already_extracted(title, site, source_url="", version=""):
    """Procura pelo mesmo anime/versão em qualquer site; não mistura dublado e legendado."""
    animes = load_animes()
    target_title = normalize_title(title).lower()
    target_version = normalize_version(version)

    # 1) Busca exata por identidade nova.
    if target_version in ("dublado", "legendado"):
        target_id = get_anime_id(title, site, source_url, target_version)
        for a in animes:
            if a.get("id") == target_id:
                return a
        for a in animes:
            if (
                normalize_title(a.get("title", "")).lower() == target_title
                and normalize_version(a.get("version", "")) == target_version
            ):
                return a
        return None

    # 2) Sem versão informada: só retorna se houver uma única versão para o título/site.
    matches = [
        a for a in animes
        if normalize_title(a.get("title", "")).lower() == target_title
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _add_or_update_single(data, force=False, version=""):
    data = dict(data)
    data["title"] = normalize_title(data.get("title"))
    data["version"] = normalize_version(version) or normalize_version(data.get("version")) or "indefinido"
    for ep in data.get("episodes", []) or []:
        ep["version"] = normalize_version(ep.get("version")) or data["version"]

    animes = load_animes()
    existing = is_already_extracted(data["title"], data["site"], data.get("source_url", ""), data["version"])

    if existing:
        updated = _merge_record(existing, data, data["version"])
        replaced = False
        for i, a in enumerate(animes):
            if a.get("id") == existing.get("id"):
                animes[i] = updated
                replaced = True
                break
        if not replaced:
            animes.append(updated)
        save_animes(animes)

        old_count = len(existing.get("episodes", []) or [])
        new_count = len(updated.get("episodes", []) or [])
        added = max(0, new_count - old_count)
        if added:
            print(f"\n  🔄 '{updated['title']}' [{updated['version']}] atualizado: +{added} eps. Total: {new_count}", flush=True)
        else:
            print(f"\n  ⚠️  '{updated['title']}' [{updated['version']}] já está atualizado ({new_count} eps).", flush=True)
        return updated

    if existing and force:
        animes = [a for a in animes if a.get("id") != existing.get("id")]
        print(f"\n  🔄 Forçando re-scrape de '{data['title']}' [{data['version']}]", flush=True)

    data = _merge_record({}, data, data["version"])
    animes.append(data)
    save_animes(animes)
    print(f"\n  ✅ '{data['title']}' [{data['version']}] adicionado ({data['total_episodes']} eps).", flush=True)
    return data


@_serialize_database_access
def add_or_update(data, force=False):
    """Salva cada versão em seu próprio registro. Nunca mistura dublado/legendado."""
    episodes = data.get("episodes", []) or []
    groups = {}
    default_version = normalize_version(data.get("version", ""))

    for ep in episodes:
        ep = dict(ep)
        version = normalize_version(ep.get("version", ""))
        if version not in ("dublado", "legendado"):
            version = default_version or guess_version_from_text(ep.get("name"), ep.get("url")) or "indefinido"
        ep["version"] = version
        groups.setdefault(version, []).append(ep)

    if not groups:
        groups[default_version or "indefinido"] = []

    results = []
    for version, version_eps in groups.items():
        part = dict(data)
        part["title"] = normalize_title(data.get("title"))
        part["version"] = version
        part["episodes"] = version_eps
        part["total_episodes"] = len(version_eps)
        results.append(_add_or_update_single(part, force=force, version=version))

    return results[0] if len(results) == 1 else results


def show_status():
    animes = load_animes()
    if not animes:
        print("  JSON vazio.")
        return
    print(f"\n  {'='*60}")
    print(f"  ANIMES NO JSON: {len(animes)} registros separados por versão")
    print(f"  {'='*60}")
    for a in animes:
        title = a.get("title", "?")
        site = a.get("site", "?")
        version = a.get("version", "?")
        eps = len(a.get("episodes", []))
        extracted = a.get("extracted_at", "N/A")
        has_img = "🖼️" if a.get("image") else "  "
        print(f"  {has_img} {title} [{site}] [{version}] — {eps} eps — {extracted}")
    print(f"  {'='*60}")


# ═══════════════════════════════════════════════════════════
# UTILITÁRIOS
# ═══════════════════════════════════════════════════════════

def get_soup(url, referer="", retries=3, timeout=30):
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            return BeautifulSoup(r.text, "html.parser")
        except requests.RequestException as e:
            last_error = e
            if attempt < retries:
                time.sleep(attempt)

    raise last_error


def test_player(url, timeout=10):
    try:
        r = requests.head(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if r.status_code == 405:
            r = requests.get(url, headers=HEADERS, timeout=timeout, stream=True)
            r.close()
        return {"status": r.status_code, "ok": r.status_code == 200, "content_type": r.headers.get("content-type", "")}
    except requests.exceptions.Timeout:
        return {"status": 0, "ok": False, "error": "timeout"}
    except Exception as e:
        return {"status": 0, "ok": False, "error": str(e)}


def extract_players(soup):
    players = []
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src") or iframe.get("data-src") or iframe.get("data-lazy-src") or ""
        if src and src.startswith("http"):
            players.append({"url": src, "type": "iframe"})
    for source in soup.find_all("source"):
        src = source.get("src", "")
        if src and src.startswith("http"):
            players.append({"url": src, "type": "source_tag"})
    for video in soup.find_all("video"):
        src = video.get("src", "")
        if src and src.startswith("http"):
            players.append({"url": src, "type": "video_tag"})
    html = str(soup)
    for m in re.finditer(r'(https?://[^\s"\'<>\\]+\.(?:m3u8|mp4))', html):
        url = m.group(1).rstrip('\\')
        if not any(p["url"] == url for p in players):
            players.append({"url": url, "type": "regex"})
    for m in re.finditer(r'(https?://[^\s"\'<>]+googlevideo\.com[^\s"\'<>]*)', html):
        url = m.group(1).rstrip('\\')
        if not any(p["url"] == url for p in players):
            players.append({"url": url, "type": "blogger_mp4"})
    seen = set()
    unique = []
    for p in players:
        if p["url"] not in seen:
            seen.add(p["url"])
            unique.append(p)
    return unique


def _looks_like_media_url(url):
    if not isinstance(url, str) or not url:
        return False
    low = url.lower()
    return ('.m3u8' in low or '.mp4' in low or 'videohls.php' in low or
            'googlevideo.com' in low or 'mime=video' in low or 'type=video' in low)


def _looks_like_player_url(url):
    """Reconhece páginas que servem de player, mesmo sem extensão de mídia."""
    if not isinstance(url, str) or not url:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    return path.startswith("/m3/") or (
        host == "00000410.xyz" and path.startswith("/m3/")
    ) or (host.endswith("anizero.org") and path.rstrip("/") == "/player")


def _extract_media_urls_from_html(html, base_url=""):
    """Extrai players, m3u8/mp4 e URLs codificadas em páginas de episódio."""
    result=[]; seen=set()
    def add(u):
        if not u: return
        try: u=urljoin(base_url or '', str(u).strip().replace('\\/','/'))
        except Exception: return
        if not u.startswith(('http://','https://')): return
        # A página carrega muitos scripts e imagens. Só conserva players e
        # arquivos de mídia para que um .js não vire a URL do episódio.
        if not _looks_like_media_url(u) and not _looks_like_player_url(u): return
        if u not in seen:
            seen.add(u); result.append(u)
    soup=BeautifulSoup(html or '', 'html.parser')
    for tag in soup.find_all(True):
        for attr in ('src','data-src','data-url','data-video','data-video-url','data-lazy-src'):
            add(tag.get(attr))
        # O AniZero pode guardar o player /m3/... em links, não apenas em iframe.
        href = tag.get('href')
        if href and '/m3/' in href.lower():
            add(href)
    patterns=[
        r'https?://[^\s"\'<>]+\.m3u8(?:\?[^\s"\'<>]*)?',
        r'https?://[^\s"\'<>]+\.mp4(?:\?[^\s"\'<>]*)?',
        r'https?://[^\s"\'<>]*googlevideo\.com[^\s"\'<>]*',
        r'https?://[^\s"\'<>]*videohls\.php\?[^\s"\'<>]*',
        r'https?://[^\s"\'<>]+/m3/[^\s"\'<>]*',
    ]
    for pat in patterns:
        for u in re.findall(pat, html or '', re.I): add(u.rstrip('\\'))
    for u in list(result):
        try:
            q=parse_qs(urlparse(u).query)
            for value in q.get('d',[]):
                decoded=unquote(value)
                if decoded.startswith(('http://','https://')): add(decoded)
        except Exception: pass
    def rank(u):
        low=u.lower()
        if 'videohls.php' in low:
            return 2
        if re.search(r'\.(?:m3u8|mp4)(?:\?|$)', low) or 'googlevideo.com' in low:
            return 0
        return 1
    result.sort(key=rank)
    return result


def _episode_has_url(ep):
    if not isinstance(ep,dict): return False
    url = ep.get('url') or ''
    if url and re.search(r'\.(?:js|css|jpe?g|png|gif|webp|svg)(?:[?#]|$)', str(url), re.I):
        return False
    if url:
        return True
    for key in ('sources','players'):
        for x in ep.get(key,[]) or []:
            candidate = x.get('url') if isinstance(x,dict) else x
            if candidate and not re.search(r'\.(?:js|css|jpe?g|png|gif|webp|svg)(?:[?#]|$)', str(candidate), re.I):
                return True
    return False


def _episode_key(ep, default_version=''):
    return (int(ep.get('season',1) or 1), int(ep.get('number',0) or 0),
            normalize_version(ep.get('version','')) or default_version or 'indefinido')


# SITE 1: ANIMESONLINECC.TO
# ═══════════════════════════════════════════════════════════

def list_all_animes_animesonlinecc():
    print("  [INFO] Listando animes do animesonlinecc.to (A-Z)...")
    all_animes = []
    seen = set()
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        url = f"https://animesonlinecc.to/anime/{letter.lower()}/"
        try:
            soup = get_soup(url)
        except Exception:
            continue
        found = 0
        for a in soup.select('a[href*="/anime/"]'):
            href = a["href"]
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = "https://animesonlinecc.to" + href
            if "/genero/" in href or "/search/" in href or "/lancamento/" in href:
                continue
            parts = href.rstrip("/").split("/")
            slug = parts[-1] if parts else ""
            if not slug or slug == "anime" or slug == "rec" or slug == letter.lower():
                continue
            if href in seen:
                continue
            seen.add(href)
            name = a.get_text(strip=True)
            if not name:
                parent = a.find_parent()
                if parent:
                    h3 = parent.find("h3")
                    if h3:
                        name = h3.get_text(strip=True)
            if not name:
                name = slug.replace("-", " ").title()
            all_animes.append({"name": name, "slug": slug, "url": href})
            found += 1
        if found > 0:
            print(f"    {letter}: {found} (total: {len(all_animes)})")
        time.sleep(0.5)
    print(f"  [OK] Total: {len(all_animes)} animes")
    return all_animes


def scrape_animesonlinecc(anime_url, max_eps=0, test=True, force=False):
    print(f"\n  [SITE] animesonlinecc.to")
    soup = get_soup(anime_url)
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else "Desconhecido"

    existing = is_already_extracted(title, "animesonlinecc.to", anime_url)
    if existing and not force:
        print(
            f"  ℹ️  '{title}' já existe no JSON "
            f"({len(existing.get('episodes', []))} eps). Verificando novidades..."
        )

    image_url = ""
    og_img = soup.select_one("meta[property='og:image']")
    if og_img and og_img.get("content"):
        image_url = og_img["content"]
    if image_url.startswith("//"):
        image_url = "https:" + image_url

    synopsis = ""
    desc_el = soup.select_one(".description p, .synopsis p, .entry-content p")
    if desc_el:
        synopsis = desc_el.get_text(strip=True)
    else:
        og_desc = soup.select_one("meta[property='og:description']")
        if og_desc:
            synopsis = og_desc["content"]

    genres = []
    for a in soup.select("a[href*='genre'], a[href*='genero'], .genre a, .genres a"):
        t = a.get_text(strip=True)
        if t and t not in genres and len(t) < 30:
            genres.append(t)

    info = {}
    for li in soup.select(".info li, .anime-info li, .details li, .meta li"):
        text = li.get_text(strip=True)
        if "Ano" in text or "Year" in text:
            info["year"] = re.sub(r'Ano|Year', '', text).strip(': ')
        elif "Estúdio" in text or "Studio" in text:
            info["studio"] = re.sub(r'Estúdio|Studio', '', text).strip(': ')

    episodes = []
    seen = set()
    for a in soup.select('a[href*="/episodio/"]'):
        url = a["href"]
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = "https://animesonlinecc.to" + url
        if url in seen:
            continue
        seen.add(url)
        name = a.get_text(strip=True)
        m = re.search(r'episodio-(\d+)', url)
        num = int(m.group(1)) if m else 0
        if num == 0:
            continue
        if "dub" in url.lower():
            version = "dublado"
        elif "leg" in url.lower():
            version = "legendado"
        elif "dub" in name.lower():
            version = "dublado"
        else:
            version = "legendado"
        episodes.append({"name": name, "number": num, "url": url, "version": version, "season": 1})

    episodes.sort(key=lambda e: (e["season"], e["number"]))
    print(f"  [INFO] {title} | {len(episodes)} episódios")

    existing_keys = set()
    if existing:
        existing_keys = {
            (
                e.get("season", 1),
                e.get("number", 0),
                e.get("version", "")
            )
            for e in existing.get("episodes", [])
        }

    eps_to_scrape = [
        e for e in episodes
        if (
            e.get("season", 1),
            e.get("number", 0),
            e.get("version", "")
        ) not in existing_keys
    ]

    if max_eps > 0:
        eps_to_scrape = eps_to_scrape[:max_eps]

    if not eps_to_scrape:
        print(f"  ✅ Nenhum episódio novo.")
        return None

    print(f"  [INFO] Scrapando {len(eps_to_scrape)} episódios...")
    results = []
    for i, ep in enumerate(eps_to_scrape):
        print(f"    [{i+1}/{len(eps_to_scrape)}] Ep {ep['number']} ({ep['version']})", end=" ", flush=True)
        try:
            ep_soup = get_soup(ep["url"], referer=anime_url)
            players = extract_players(ep_soup)
            if test and players:
                for p in players:
                    p["test"] = test_player(p["url"])
            results.append({**ep, "players": players, "player_count": len(players)})
            print(f"-> {len(players)} player(s)")
        except Exception as e:
            print(f"ERRO: {e}")
            results.append({**ep, "players": [], "player_count": 0, "error": str(e)})
        time.sleep(0.5)

    data = {"site": "animesonlinecc.to", "title": title, "image": image_url, "synopsis": synopsis,
            "genres": genres, "info": info, "episodes": results,
            "total_episodes": len(results), "source_url": anime_url}
    add_or_update(data, force)
    return data


def scrape_entire_animesonlinecc(max_animes=0, max_eps=0, test=False, force=False):
    animes = list_all_animes_animesonlinecc()
    if max_animes > 0:
        animes = animes[:max_animes]
    print(f"\n  [INFO] Scrapando {len(animes)} animes...")
    for i, anime in enumerate(animes):
        print(f"\n  [{i+1}/{len(animes)}] {anime['name']}")
        try:
            scrape_animesonlinecc(anime["url"], max_eps, test, force)
        except Exception as e:
            print(f"    [ERRO] {e}")
        time.sleep(1)


# ═══════════════════════════════════════════════════════════
def _extract_animesonlinecloud_listing(soup, base_url):
    """Extrai cards de títulos da categoria pública de legendados."""
    results, seen = [], set()
    for a in soup.select('a[href]'):
        href = urljoin(base_url, a.get('href', '').strip())
        parsed = urlparse(href)
        if parsed.hostname not in ('animesonline.cloud', 'www.animesonline.cloud'):
            continue
        if not re.match(r'^/anime/[^/]+/?$', parsed.path, re.I):
            continue
        href = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', ''))
        if href in seen:
            continue
        card = a.find_parent(['article', 'li']) or a.parent
        heading = card.select_one('h2, h3, h4, [class*="title"]') if card else None
        img = a.select_one('img') or (card.select_one('img') if card else None)
        title = (a.get('title') or (heading.get_text(' ', strip=True) if heading else '')
                 or a.get_text(' ', strip=True))
        image = ''
        if img:
            image = img.get('data-src') or img.get('data-lazy-src') or img.get('data-original') or img.get('src') or ''
            if not image and img.get('srcset'):
                image = img['srcset'].split(',')[0].strip().split(' ')[0]
            image = urljoin(base_url, image)
        if not title:
            title = parsed.path.rstrip('/').split('/')[-1].replace('-', ' ').title()
        seen.add(href)
        results.append({'title': title, 'url': href, 'image': image})
    return results


def _extract_animesonlinecloud_episode_links(soup, anime_url):
    """Coleta apenas links públicos para as páginas dos episódios."""
    episodes, seen = [], set()
    for a in soup.select('a[href]'):
        href = urljoin(anime_url, a.get('href', '').strip())
        parsed = urlparse(href)
        label = ' '.join([a.get_text(' ', strip=True), a.get('title', ''),
                          a.get('aria-label', ''), parsed.path]).strip()
        match = (re.search(r'(?:epis[oó]dio|episode|ep)\s*[-:# ]*0*(\d{1,4})\b', label, re.I)
                 or re.search(r'\b0*(\d{1,4})\s*(?:epis[oó]dio|episode)\b', label, re.I))
        if not match or parsed.hostname not in ('animesonline.cloud', 'www.animesonline.cloud'):
            continue
        if not re.search(r'/(?:episodio|episode|video)/', parsed.path, re.I):
            continue
        number = int(match.group(1))
        if not 1 <= number <= 10000 or href in seen:
            continue
        seen.add(href)
        episodes.append({'name': a.get_text(' ', strip=True) or f'Episódio {number}',
                         'number': number, 'season': 1, 'version': 'legendado',
                         'episode_url': href})
    return sorted(episodes, key=lambda ep: ep['number'])


def _extract_episode_iframes(soup, episode_url):
    """Obtém iframes de reprodução publicados na página do episódio."""
    players, seen = [], set()
    for frame in soup.select('.player iframe, #player iframe, .video-player iframe, iframe[src], iframe[data-src], iframe[data-lazy-src]'):
        src = (frame.get('src') or frame.get('data-src') or frame.get('data-lazy-src') or '').strip()
        if not src or src.startswith(('javascript:', 'data:')):
            continue
        src = urljoin(episode_url, src)
        parsed = urlparse(src)
        if parsed.scheme not in ('http', 'https') or src in seen:
            continue
        if re.search(r'(^|\.)(?:doubleclick\.net|googlesyndication\.com|adservice\.google\.com|facebook\.com)$', parsed.hostname or '', re.I):
            continue
        seen.add(src)
        players.append({'url': src, 'type': 'iframe'})
    return players


def _attach_episode_iframes(episodes, anime_url, retries=3, timeout=30):
    """Lê as páginas de episódio e guarda os iframes do player publicado."""
    for index, episode in enumerate(episodes, 1):
        print(f'    [EP {index}/{len(episodes)}] Buscando player do episÃ³dio {episode["number"]}...', flush=True)
        try:
            page = get_soup(episode['episode_url'], referer=anime_url,
                            retries=retries, timeout=timeout)
            episode['players'] = _extract_episode_iframes(page, episode['episode_url'])
            episode['player_count'] = len(episode['players'])
            print(f'    [EP {index}/{len(episodes)}] Player(s): {episode["player_count"]}', flush=True)
        except Exception as exc:
            episode['players'] = []
            episode['player_count'] = 0
            episode['player_error'] = str(exc)
            print(f'    [EP {index}/{len(episodes)}] Falhou ao buscar player: {type(exc).__name__}: {exc}', flush=True)
        time.sleep(0.3)
    return episodes


def scrape_animesonlinecloud(max_animes=0):
    """Percorre todas as páginas da categoria legendada."""
    base = 'https://animesonline.cloud/tipo/legendado'
    listing, seen_urls, max_page = [], set(), 1
    for page_num in range(1, 1001):
        page_url = base if page_num == 1 else f'{base}/page/{page_num}'
        try:
            soup = get_soup(page_url, retries=1, timeout=20)
        except Exception as exc:
            print(f'  [AVISO] Página {page_num}: {exc}')
            break
        for a in soup.select('a[href]'):
            match = re.search(r'/page/(\d+)/?$', urlparse(urljoin(page_url, a.get('href', ''))).path, re.I)
            if match:
                max_page = max(max_page, int(match.group(1)))
        added = 0
        for item in _extract_animesonlinecloud_listing(soup, page_url):
            if item['url'] not in seen_urls:
                seen_urls.add(item['url'])
                listing.append(item)
                added += 1
        print(f'  [LISTA] Página {page_num}/{max_page}: +{added} (total {len(listing)})')
        if max_animes and len(listing) >= max_animes:
            listing = listing[:max_animes]
            break
        if page_num >= max_page or added == 0:
            break
        time.sleep(0.4)

    print(f'  [INFO] Coletando metadados de {len(listing)} títulos.')
    for index, item in enumerate(listing, 1):
        print(f'  [{index}/{len(listing)}] Abrindo {item["title"]}...', flush=True)
        try:
            print(f'    [PAGINA] Baixando detalhes: {item["url"]}', flush=True)
            soup = get_soup(item['url'], retries=1, timeout=20)
            h1 = soup.find('h1')
            title = h1.get_text(' ', strip=True) if h1 else item['title']
            meta_image = soup.select_one('meta[property="og:image"], meta[name="twitter:image"]')
            image = urljoin(item['url'], meta_image.get('content', '')) if meta_image else item['image']
            if not image:
                poster = soup.select_one('.poster img, .cover img, .thumbnail img, img[data-src], img[src]')
                if poster:
                    image = urljoin(item['url'], poster.get('data-src') or poster.get('data-lazy-src') or poster.get('src') or '')
            meta_desc = soup.select_one('meta[property="og:description"], meta[name="description"]')
            synopsis = meta_desc.get('content', '').strip() if meta_desc else ''
            genres = []
            for tag in soup.select('.genres a, .genre a, a[href*="/genero/"]'):
                value = tag.get_text(' ', strip=True)
                if value and value not in genres:
                    genres.append(value)
            episodes = _extract_animesonlinecloud_episode_links(soup, item['url'])
            print(f'    [EPISODIOS] Encontrados {len(episodes)}; buscando links dos players...', flush=True)
            episodes = _attach_episode_iframes(episodes, item['url'], retries=1, timeout=20)
            data = {'site': 'animesonline.cloud', 'title': normalize_title(title),
                    'version': 'legendado', 'image': image, 'synopsis': synopsis,
                    'genres': genres, 'info': {}, 'episodes': episodes,
                    'total_episodes': len(episodes), 'source_url': item['url']}
            add_or_update(data)
            iframe_count = sum(ep.get('player_count', 0) for ep in episodes)
            print(f'  [{index}/{len(listing)}] {title}: capa={"sim" if image else "não"}, episódios={len(episodes)}, players={iframe_count}')
        except Exception as exc:
            print(f'  [{index}/{len(listing)}] {item["title"]}: ERRO {type(exc).__name__}: {exc}')
        time.sleep(0.35)


def _extract_animesonlines_listing(soup, base_url):
    """Extrai títulos, imagens e marcações DUB/LEG do catálogo."""
    results, seen = [], set()
    for a in soup.select('a[href]'):
        href = urljoin(base_url, a.get('href', '').strip())
        parsed = urlparse(href)
        if parsed.hostname not in ('animesonlines.net', 'www.animesonlines.net'):
            continue
        if not re.match(r'^/anime/[^/]+/?$', parsed.path, re.I):
            continue
        href = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip('/') + '/', '', ''))
        if href in seen:
            continue
        card = a.find_parent(['article', 'li']) or a.parent
        heading = card.select_one('h2, h3, h4, [class*="title"]') if card else None
        img = a.select_one('img') or (card.select_one('img') if card else None)
        title = (heading.get_text(' ', strip=True) if heading else '') or a.get('title', '') or a.get_text(' ', strip=True)
        card_text = card.get_text(' ', strip=True) if card else a.get_text(' ', strip=True)
        version = guess_version_from_text(card_text, title, href)
        if re.search(r'\bDUB\b', card_text, re.I):
            version = 'dublado'
        elif re.search(r'\bLEG\b', card_text, re.I):
            version = 'legendado'
        image = ''
        if img:
            image = img.get('data-src') or img.get('data-lazy-src') or img.get('data-original') or img.get('src') or ''
            if not image and img.get('srcset'):
                image = img['srcset'].split(',')[0].strip().split(' ')[0]
            image = urljoin(base_url, image)
        if not title:
            title = parsed.path.strip('/').split('/')[-1].replace('-', ' ').title()
        seen.add(href)
        results.append({'title': title, 'url': href, 'image': image,
                        'version': version or 'indefinido'})
    return results


def _extract_animesonlines_episode_links(soup, anime_url, version):
    """Retorna links das páginas de episódios, sem acessar os players."""
    episodes, seen = [], set()
    for a in soup.select('a[href*="/episodio/"]'):
        href = urljoin(anime_url, a.get('href', '').strip())
        parsed = urlparse(href)
        if parsed.hostname not in ('animesonlines.net', 'www.animesonlines.net') or href in seen:
            continue
        label = ' '.join([a.get_text(' ', strip=True), a.get('title', ''),
                          a.get('aria-label', ''), parsed.path]).strip()
        match = (re.search(r'(?:epis[oó]dio|episode|ep)\s*[-:#. ]*0*(\d{1,4})\b', label, re.I)
                 or re.search(r'\b0*(\d{1,4})\s*(?:epis[oó]dio|episode)\b', label, re.I))
        if not match:
            continue
        number = int(match.group(1))
        if not 1 <= number <= 10000:
            continue
        seen.add(href)
        episodes.append({'name': a.get_text(' ', strip=True) or f'Episódio {number}',
                         'number': number, 'season': 1, 'version': version,
                         'episode_url': href})
    return sorted(episodes, key=lambda ep: ep['number'])


def scrape_animesonlines_net(max_animes=0):
    """Percorre todas as páginas do catálogo e salva metadados públicos."""
    base = 'https://animesonlines.net/lista-de-animes/'
    listing, seen_urls, max_page = [], set(), 1
    for page_num in range(1, 1001):
        page_url = base if page_num == 1 else f'{base}?pagina={page_num}'
        try:
            soup = get_soup(page_url)
        except Exception as exc:
            print(f'  [AVISO] Página {page_num}: {exc}')
            break
        for a in soup.select('a[href]'):
            parsed = urlparse(urljoin(page_url, a.get('href', '')))
            for value in parse_qs(parsed.query).get('pagina', []):
                if value.isdigit():
                    max_page = max(max_page, int(value))
        added = 0
        for item in _extract_animesonlines_listing(soup, page_url):
            key = (item['url'], item['version'])
            if key not in seen_urls:
                seen_urls.add(key)
                listing.append(item)
                added += 1
        print(f'  [LISTA] Página {page_num}/{max_page}: +{added} (total {len(listing)})')
        if max_animes and len(listing) >= max_animes:
            listing = listing[:max_animes]
            break
        if page_num >= max_page or added == 0:
            break
        time.sleep(0.4)

    print(f'  [INFO] Coletando metadados de {len(listing)} itens do catálogo.')
    for index, item in enumerate(listing, 1):
        try:
            soup = get_soup(item['url'])
            h1 = soup.find('h1')
            title = h1.get_text(' ', strip=True) if h1 else item['title']
            version = item['version']
            if version not in ('dublado', 'legendado'):
                version = guess_version_from_text(title, item['url']) or 'indefinido'
            meta_image = (soup.select_one('meta[property="og:image"]')
                          or soup.select_one('meta[name="twitter:image"]'))
            image = urljoin(item['url'], meta_image.get('content', '')) if meta_image else item['image']
            if not image:
                poster = soup.select_one('.poster img, .cover img, [class*="poster"] img, img[data-src], img[src]')
                if poster:
                    image = urljoin(item['url'], poster.get('data-src') or poster.get('data-lazy-src') or poster.get('src') or '')
            meta_desc = soup.select_one('meta[property="og:description"], meta[name="description"]')
            synopsis = meta_desc.get('content', '').strip() if meta_desc else ''
            if not synopsis:
                desc = soup.select_one('.synopsis, .description, [class*="sinopse"]')
                synopsis = desc.get_text(' ', strip=True) if desc else ''
            genres = []
            for tag in soup.select('.genres a, .genre a, a[href*="/genero/"], a[href*="/genre/"]'):
                value = tag.get_text(' ', strip=True)
                if value and value not in genres:
                    genres.append(value)
            episodes = _extract_animesonlines_episode_links(soup, item['url'], version)
            episodes = _attach_episode_iframes(episodes, item['url'])
            data = {'site': 'animesonlines.net', 'title': normalize_title(title),
                    'version': version, 'image': image, 'synopsis': synopsis,
                    'genres': genres, 'info': {}, 'episodes': episodes,
                    'total_episodes': len(episodes), 'source_url': item['url']}
            add_or_update(data)
            iframe_count = sum(ep.get('player_count', 0) for ep in episodes)
            print(f'  [{index}/{len(listing)}] {title} [{version}]: capa={"sim" if image else "não"}, episódios={len(episodes)}, players={iframe_count}')
        except Exception as exc:
            print(f'  [{index}/{len(listing)}] {item["title"]}: ERRO {type(exc).__name__}: {exc}')
        time.sleep(0.35)


# SITES 2/3: PLAYWRIGHT HEADLESS + AJAX ANIZERO
# ═══════════════════════════════════════════════════════════

async def get_anizero_search_nonce_pw(page):
    """Obtém o token search_nonce inspecionando scripts, requisições e variáveis do AniZero."""
    nonce = None

    def handle_request(request):
        nonlocal nonce
        if "admin-ajax.php" in request.url and request.method == "POST":
            post_data = request.post_data
            if post_data and "search_nonce=" in post_data:
                m = re.search(r'search_nonce=([^&]+)', post_data)
                if m:
                    nonce = m.group(1)

    page.on("request", handle_request)

    try:
        await page.goto("https://anizero.org/lista-de-animes/", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1500)

        if nonce:
            return nonce

        nonce = await page.evaluate(r"""
            () => {
                if (window.js_global && window.js_global.search_nonce) {
                    return window.js_global.search_nonce;
                }
                if (window.anime_obj && window.anime_obj.nonce) {
                    return window.anime_obj.nonce;
                }
                
                const scripts = Array.from(document.querySelectorAll('script'));
                for (const s of scripts) {
                    const content = s.textContent || '';
                    const m = content.match(/["']search_nonce["']\s*:\s*["']([^"'\s]+)["']/);
                    if (m) return m[1];
                    const m2 = content.match(/["']nonce["']\s*:\s*["']([^"'\s]+)["']/);
                    if (m2) return m2[1];
                }

                const html = document.documentElement.innerHTML;
                const match = html.match(/["']search_nonce["']\s*:\s*["']([^"'\s]+)["']/);
                return match ? match[1] : null;
            }
        """)
        if not nonce:
            # O tema injeta js_global por uma URL de script independente.
            nonce = await page.evaluate("""async () => {
                try {
                    const response = await fetch('/?js_global=1&ver=7.1.2', {credentials:'same-origin'});
                    const text = await response.text();
                    const match = text.match(/search_nonce\\s*:\\s*['\"]([^'\"]+)['\"]/);
                    return match ? match[1] : null;
                } catch(e) { return null; }
            }""")
        return nonce
    except Exception as e:
        print(f"  [ERRO] Falha ao obter search_nonce via Playwright: {e}")
        return None


def fetch_anizero_ajax_page(nonce, page=1):
    """Pede os dados em JSON de uma página via AJAX."""
    ajax_url = "https://anizero.org/wp-admin/admin-ajax.php"
    headers = dict(HEADERS)
    headers["X-Requested-With"] = "XMLHttpRequest"
    headers["Referer"] = "https://anizero.org/lista-de-animes/"
    payload = {
        "action": "show_animes_ajax",
        "search_nonce": nonce,
        "letra": "",
        "paged": str(page),
    }
    try:
        r = requests.post(ajax_url, data=payload, headers=headers, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [ERRO] Falha ao requisitar pág {page} via AJAX: {e}")
        return None


async def _collect_episodes_with_pagination(page):
    """Percorre todas as partes/páginas do anime e coleta links /video/... reais."""
    all_eps=[]; seen_urls=set(); seen_numbers=set(); visited=set(); queue=[page.url]
    for _ in range(200):
        if not queue: break
        current=queue.pop(0)
        if current in visited: continue
        visited.add(current)
        try:
            if page.url!=current:
                await page.goto(current,wait_until='domcontentloaded',timeout=60000)
            await page.wait_for_timeout(1000)
        except Exception as e:
            print(f"    [ANIZERO] erro ao abrir parte: {e}",flush=True); continue
        data=await page.evaluate(r"""async () => {
            const abs=u=>{try{return new URL(u,location.href).href}catch(e){return ''}};
            const eps=[]; const epSeen=new Set();
            // A lista atual do AniZero carrega os episódios por POST em /api/.
            const scripts=Array.from(document.scripts).map(s=>s.textContent||'').join('\n');
            const idMatch=scripts.match(/\banime_id\s*=\s*(\d+)/);
            const apiMatch=scripts.match(/rteam_api_url\s*=\s*['\"]([^'\"]+)/);
            if(idMatch && apiMatch){
                try{
                    const body=new URLSearchParams({action:'show_videos',anime_id:idMatch[1]});
                    const response=await fetch(apiMatch[1],{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8'},body:body.toString(),credentials:'same-origin'});
                    const videos=await response.json();
                    for(const item of Object.values(videos.episodios||{})){
                        const n=parseInt(item.epi_num,10); const href=abs(item.epi_url||'');
                        if(!n||n<1||n>10000||!href||epSeen.has(href)) continue;
                        epSeen.add(href);
                        eps.push({number:n,name:item.epi_title||`Episódio ${n}`,url:href});
                    }
                }catch(e){}
            }
            document.querySelectorAll('a[href]').forEach(a=>{
                const txt=(a.textContent||'').replace(/\s+/g,' ').trim(); const href=abs(a.href||'');
                if(!href) return;
                let m=(txt+' '+href).match(/(?:epis[oó]dio|episode)\s*0*(\d{1,4})\b/i)
                    || txt.match(/\b0*(\d{1,4})\s*(?:epis[oó]dio|episode)\b/i);
                if(!m) return;
                const n=parseInt(m[1],10); if(!n||n<1||n>10000) return;
                if(!/\/(?:video)\//i.test(href)) return;
                if(epSeen.has(href)) return; epSeen.add(href);
                eps.push({number:n,name:txt.substring(0,120)||`Episódio ${n}`,url:href});
            });
            const pages=[]; const pseen=new Set(); const addPage=a=>{const h=abs(a.href||a.getAttribute('href'));if(h&&h!==location.href&&!pseen.has(h)){pseen.add(h);pages.push(h)}};
            document.querySelectorAll('a[rel="next"],.pagination a[href],.page-numbers[href],.nav-links a[href],nav.pagination a[href]').forEach(addPage);
            document.querySelectorAll('a[href]').forEach(a=>{
                const txt=((a.textContent||'')+' '+(a.getAttribute('aria-label')||'')+' '+(a.getAttribute('title')||'')).toLowerCase(); const h=a.href||'';
                let path=''; try{path=new URL(h).pathname}catch(e){}
                if(/\/(?:page|pagina)[-_]?\d+\/?$/i.test(path)||/[?&](?:page|paged|pagina)=\d+/i.test(h)||/(?:próxima|proxima|next|seguinte|mais episódios|mais episodios|carregar mais|load more)/i.test(txt)) addPage(a);
            });
            return {eps,pages};
        }""")
        fresh=0
        for ep in data.get('eps',[]):
            if ep['url'] in seen_urls: continue
            seen_urls.add(ep['url'])
            if ep['number'] not in seen_numbers:
                seen_numbers.add(ep['number']); all_eps.append(ep); fresh+=1
            else:
                # Mesmo número pode ter URL diferente; conserva URL adicional no name list later.
                all_eps.append(ep); fresh+=1
        print(f"    [ANIZERO] Parte {len(visited)}: +{fresh} URLs de episódios (total {len(all_eps)})",flush=True)
        for u in data.get('pages',[]):
            if u not in visited and u not in queue: queue.append(u)
    # Deduplica por número preferindo URL única, mas mantém a primeira página real.
    by_num={}
    for ep in all_eps:
        n=ep['number']
        by_num.setdefault(n,ep)
    return [by_num[n] for n in sorted(by_num)]


async def _detect_version(page, title):
    return await page.evaluate(r"""
        (animeTitle) => {
            const detect = value => {
                const text = String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
                const dub = /\b(?:dublado|dublada|dubbed|dub)\b/.test(text);
                const leg = /\b(?:legendado|legendada|leg|sub)\b/.test(text);
                if (dub === leg) return '';
                return dub ? 'dublado' : 'legendado';
            };
            const controls = document.querySelectorAll('button, a, [role="tab"], [role="radio"], [aria-pressed], [class*="version"], [class*="lang"], [class*="audio"]');
            for (const el of controls) {
                const active = el.classList.contains('active') || el.classList.contains('selected')
                    || el.classList.contains('on') || el.classList.contains('current')
                    || el.getAttribute('aria-selected') === 'true'
                    || el.getAttribute('aria-pressed') === 'true'
                    || el.getAttribute('aria-checked') === 'true';
                if (!active) continue;
                const result = detect([el.textContent, el.getAttribute('aria-label'), el.title].join(' '));
                if (result) return result;
            }
            const titleVersion = detect(animeTitle);
            if (titleVersion) return titleVersion;
            return detect(window.location.href) || 'indefinido';
        }
    """, title)

async def _scrape_single_anime_pw(page, context, anime_url, site_domain, test, force, max_eps, version_hint=""):
    await page.goto(anime_url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(3000)

    title = ""
    try:
        title = await page.eval_on_selector("h1", "el => el.textContent.trim()")
    except Exception:
        pass

    if not title:
        try:
            title = await page.title()
        except Exception:
            title = "Desconhecido"

    title = re.sub(r"\s+", " ", title).strip() or "Desconhecido"

    detected_version = await _detect_version(page, title)
    hint_version = normalize_version(version_hint)
    url_version = guess_version_from_text(anime_url)
    version = hint_version if hint_version in ("dublado", "legendado") else (
        detected_version if detected_version in ("dublado", "legendado") else
        url_version if url_version in ("dublado", "legendado") else "indefinido"
    )
    # Exce??o confirmada: essa p?gina do AniZero representa a vers?o dublada.
    if site_domain == "anizero.org" and "/baki-o-campeao" in urlparse(anime_url).path.lower():
        version = "dublado"
    existing = is_already_extracted(title, site_domain, anime_url, version)
    if existing and not force:
        missing_urls = sum(1 for e in existing.get('episodes', []) or [] if not _episode_has_url(e))
        print(
            f"  ℹ️  '{title}' [{version}] já existe no JSON "
            f"({len(existing.get('episodes', []))} eps; {missing_urls} sem URL). Verificando novidades...", flush=True
        )

    image_url = await page.evaluate(r"""
        () => {
            const og = document.querySelector('meta[property="og:image"]');
            if (og) return og.content;
            const img = document.querySelector('img');
            if (img && img.naturalWidth > 100) return img.src;
            return '';
        }
    """)

    synopsis = await page.evaluate(r"""
        () => {
            const og = document.querySelector('meta[property="og:description"]');
            if (og) return og.content;
            return '';
        }
    """)

    genres = await page.evaluate(r"""
        () => {
            const results = [];
            document.querySelectorAll('a[href*="genre"], a[href*="genero"], .tag, .genres a').forEach(a => {
                const t = a.textContent.trim();
                if (t && t.length < 30 && !results.includes(t)) results.push(t);
            });
            return results;
        }
    """)

    # Save basic anime data before episode/player extraction, which can fail.
    add_or_update({"site": site_domain, "title": title, "version": version,
                   "image": image_url, "synopsis": synopsis, "genres": genres,
                   "info": {}, "episodes": [], "total_episodes": 0,
                   "source_url": anime_url}, force=False)

    episodes_data = await _collect_episodes_with_pagination(page)

    seen_nums = set()
    unique_eps = []
    for ep in episodes_data:
        if ep["number"] not in seen_nums:
            seen_nums.add(ep["number"])
            unique_eps.append(ep)
    episodes_data = sorted(unique_eps, key=lambda e: e["number"])

    print(f"  [INFO] {title} | {len(episodes_data)} episódios ({version})", flush=True)

    existing_map = {}
    if existing:
        for old_ep in existing.get('episodes', []) or []:
            old_ep = dict(old_ep); old_ep['version']=normalize_version(old_ep.get('version')) or version
            existing_map[_episode_key(old_ep, version)] = old_ep

    eps_to_scrape=[]
    for ep in episodes_data:
        candidate=dict(ep); candidate['season']=1; candidate['version']=version
        old_ep=existing_map.get(_episode_key(candidate, version))
        # Corrige JSON antigo: se o episódio não tem URL/player, ele volta a ser processado.
        needs_anizero_player_refresh = False
        if site_domain == 'anizero.org' and old_ep:
            saved_urls = [old_ep.get('url', '')]
            for field in ('sources', 'players'):
                saved_urls.extend(
                    item.get('url', '') if isinstance(item, dict) else item
                    for item in old_ep.get(field, []) or []
                )
            has_m3_player = any(
                isinstance(u, str) and urlparse(u).path.lower().startswith('/m3/')
                for u in saved_urls
            )
            has_direct_media = any(
                isinstance(u, str) and _looks_like_media_url(u)
                and not _looks_like_player_url(u)
                for u in saved_urls
            )
            needs_anizero_player_refresh = not (has_m3_player or has_direct_media)
        if old_ep is None or not _episode_has_url(old_ep) or needs_anizero_player_refresh:
            eps_to_scrape.append(candidate)

    if max_eps > 0:
        eps_to_scrape = eps_to_scrape[:max_eps]

    if not eps_to_scrape:
        print(f"  ✅ Nenhum episódio novo.")
        data = {"site": site_domain, "title": title, "version": version,
                "image": image_url, "synopsis": synopsis, "genres": genres,
                "info": {}, "episodes": [], "total_episodes": 0,
                "source_url": anime_url}
        add_or_update(data, force=False)
        return data

    print(f"  [INFO] Scrapando {len(eps_to_scrape)} episódios...")
    episodes = []

    for i, ep in enumerate(eps_to_scrape):
        print(f"    [{i+1}/{len(eps_to_scrape)}] Ep {ep['number']}", end=" ", flush=True)
        try:
            if ep["url"]:
                await page.goto(ep["url"], wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2000)
            else:
                # A paginação pode retornar itens sem URL. Nesse caso,
                # tenta localizar o episódio novamente na página atual.
                await page.evaluate(
                    """(number) => {
                        const articles = document.querySelectorAll(
                            'article.group.cursor-pointer'
                        );
                        for (const article of articles) {
                            const text = article.textContent || '';
                            const match = text.match(/(\\d+)/);
                            if (match && parseInt(match[1]) === number) {
                                article.click();
                                return true;
                            }
                        }
                        return false;
                    }""",
                    ep["number"]
                )

            sources = []
            players = []
            captured = []

            def on_request(req):
                u=req.url
                if (_looks_like_media_url(u) or _looks_like_player_url(u)) and u not in captured:
                    captured.append(u)

            page.on('request', on_request)
            try:
                # Episódio: iframe/player + HTML.
                found_players = await page.evaluate(r"""() => {
                    const out=[];
                    document.querySelectorAll('iframe[src],embed[src],source[src],video[src],[data-src],[data-url],[data-video],[data-video-url],a[href*="/m3/"]').forEach(el=>{
                        const u=el.src||el.getAttribute('src')||el.getAttribute('data-src')||el.getAttribute('data-url')||el.getAttribute('data-video')||el.getAttribute('data-video-url')||el.href||'';
                        if(u&&!out.includes(u)) out.push(u);
                    });
                    document.querySelectorAll('[data-src],[data-url],[data-video],[data-video-url]').forEach(el=>{
                        const u=el.getAttribute('data-src')||el.getAttribute('data-url')||el.getAttribute('data-video')||el.getAttribute('data-video-url')||'';
                        if(u&&!out.includes(u)) out.push(new URL(u,location.href).href);
                    });
                    return out;
                }""")
                players.extend(found_players)
                # O /m3/ pode redirecionar ou carregar como frame antes de a
                # página terminar de montar o DOM. Lê também as URLs dos frames.
                for frame in page.frames:
                    frame_url = frame.url
                    if _looks_like_player_url(frame_url) and frame_url not in players:
                        players.append(frame_url)
                page_html = await page.content()
                extracted = _extract_media_urls_from_html(page_html,page.url)
                players.extend(u for u in extracted if _looks_like_player_url(u))
                sources.extend(u for u in extracted if not _looks_like_player_url(u))

                # AniZero normalmente entrega /player?id=...; abre esse player e captura
                # o m3u8/mp4 gerado pelo JavaScript/network, sem depender da primeira parte.
                for player_url in list(players):
                    try:
                        pp=await context.new_page()
                        captured_player=[]
                        def on_player_request(req):
                            if (_looks_like_media_url(req.url) or _looks_like_player_url(req.url)) and req.url not in captured_player:
                                captured_player.append(req.url)
                        pp.on('request',on_player_request)
                        try:
                            await pp.goto(player_url,wait_until='domcontentloaded',timeout=45000)
                            await pp.wait_for_timeout(5000)
                            players.extend(u for u in captured_player if _looks_like_player_url(u))
                            sources.extend(u for u in captured_player if not _looks_like_player_url(u))
                            for frame in pp.frames:
                                frame_url = frame.url
                                if _looks_like_player_url(frame_url) and frame_url not in players:
                                    players.append(frame_url)
                            player_html = await pp.content()
                            player_extracted = _extract_media_urls_from_html(player_html,pp.url)
                            players.extend(u for u in player_extracted if _looks_like_player_url(u))
                            sources.extend(u for u in player_extracted if not _looks_like_player_url(u))
                            nested=await pp.evaluate(r"""() => Array.from(document.querySelectorAll('iframe[src],source[src],video[src]')).map(x=>x.src).filter(Boolean)""")
                            for u in nested:
                                if u not in players: players.append(u)
                        finally:
                            pp.remove_listener('request',on_player_request)
                            await pp.close()
                    except Exception as e:
                        print(f"player({type(e).__name__})",end=' ',flush=True)
                players.extend(u for u in captured if _looks_like_player_url(u))
                sources.extend(u for u in captured if not _looks_like_player_url(u))
            finally:
                page.remove_listener('request',on_request)

            sources=list(_unique_values(sources))
            players=list(_unique_values(players))
            # Prefere o player real do AniZero (por exemplo 00000410.xyz/m3/...)
            # ao wrapper /player do WordPress quando nenhuma mídia direta apareceu.
            players.sort(key=lambda u: 0 if urlparse(u).path.lower().startswith('/m3/') else 1)
            direct=[u for u in sources if 'videohls.php' not in u.lower() and (re.search(r'\.(?:m3u8|mp4)(?:\?|$)',u,re.I) or 'googlevideo.com' in u.lower())]
            m3_player=next((u for u in players if urlparse(u).path.lower().startswith('/m3/')), '')
            src=direct[0] if direct else (m3_player or (sources[0] if sources else (players[0] if players else '')))
            ok=None
            if test and src and _looks_like_media_url(src):
                ok=test_player(src).get('ok')

            if src:
                print(f"{'OK' if ok else '?'} ({len(sources)} mídia(s), {len(players)} player(s))",flush=True)
                episodes.append({'number':ep['number'],'season':1,'version':version,'name':ep['name'],
                                 'url':src,'sources':sources,'players':[{'url':u} for u in players],
                                 'episode_url':page.url,'ok':ok})
            else:
                print('SEM URL DE MÍDIA',flush=True)
                episodes.append({'number':ep['number'],'season':1,'version':version,'name':ep['name'],
                                 'url':players[0] if players else '','sources':sources,
                                 'players':[{'url':u} for u in players], 'episode_url':page.url,'ok':False})
            await asyncio.sleep(0.3)
        except Exception as e:
            print(f"ERRO: {e}",flush=True)
            episodes.append({'number':ep['number'],'season':1,'version':version,'name':ep['name'],
                             'url':'','sources':[],'players':[],'episode_url':page.url,'ok':False})

    data = {"site": site_domain, "title": title, "image": image_url, "synopsis": synopsis,
            "genres": genres, "info": {}, "episodes": episodes,
            "total_episodes": len(episodes), "source_url": anime_url}
    add_or_update(data, force)
    return data


async def _scrape_pw_site(anime_url, site_name, site_domain, test=True, force=False, max_eps=0, scrape_all_site=False, max_animes=0):
    if not HAS_PLAYWRIGHT:
        raise ImportError("Playwright não instalado.")

    print(f"\n  [SITE] {site_name} (Playwright headless)")

    # AniZero: Listagem completa rápida via AJAX
    if scrape_all_site and site_domain == "anizero.org":
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=UA, viewport={"width": 1920, "height": 1080})
            page = await context.new_page()

            print("  [INFO] Obtendo token search_nonce do AniZero via Playwright...")
            nonce = await get_anizero_search_nonce_pw(page)

            if not nonce:
                print("  [ERRO] Não foi possível obter o token para o AniZero.")
                await browser.close()
                return

            print(f"  [OK] Token obtido: {nonce}")

            all_links = []
            seen = set()
            page_num = 1
            total_pages = 1

            while True:
                data = fetch_anizero_ajax_page(nonce, page_num)
                if not data:
                    break

                if "total_pages" in data:
                    total_pages = int(data.get("total_pages", 1))

                animes = data.get("animes", [])
                if not animes:
                    break

                new_count = 0
                for item in animes:
                    url = item.get("anime_permalink") or item.get("url") or item.get("link") or ""
                    name = unescape(item.get("anime_title") or item.get("title") or item.get("name") or "Sem Título")
                    if url and url not in seen:
                        seen.add(url)
                        all_links.append({"name": name, "url": url})
                        new_count += 1

                print(f"    Página {page_num}/{total_pages}: {new_count} novos (total acumulado: {len(all_links)})")

                if (max_animes > 0 and len(all_links) >= max_animes) or page_num >= total_pages:
                    break

                page_num += 1
                await asyncio.sleep(0.3)

            if max_animes > 0:
                all_links = all_links[:max_animes]

            total_encontrados = len(all_links)
            print(f"\n  ============================================================")
            print(f"  [SUCESSO] Total de {total_encontrados} animes encontrados no AniZero!")
            print(f"  ============================================================\n")

            for i, link in enumerate(all_links):
                print(f"\n  [ANIME {i+1}/{total_encontrados}] Processando: '{link['name']}'")
                print(f"  URL: {link['url']}")
                try:
                    await _scrape_single_anime_pw(page, context, link['url'], site_domain, test, force, max_eps)
                except Exception as e:
                    print(f"    [ERRO] Falha ao extrair '{link['name']}': {e}")
                await asyncio.sleep(2)

            await browser.close()
        return

    # Outros sites ou anime individual
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=UA, viewport={"width": 1920, "height": 1080})
        page = await context.new_page()

        if scrape_all_site:
            list_url = f"https://{site_domain}/animes"
            print(f"  [INFO] Lista: {list_url}")

            all_links = []
            seen_urls = set()

            for pg in range(1, 50):
                page_url = f"{list_url}?pagina={pg}" if pg > 1 else list_url

                print(f"  [INFO] Lista pág {pg}")
                try:
                    await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(3000)
                except Exception as e:
                    print(f"  [WARN] Pág {pg}: {e}")
                    break

                anime_links = await page.evaluate(r"""
                    () => {
                        const links = [];
                        const seen = new Set();
                        document.querySelectorAll('a[href*="/anime/"]').forEach(a => {
                            const href = a.href;
                            if (seen.has(href)) return;
                            const card = a.closest('[class*="card"], [class*="item"], [class*="anime"]');
                            let text = '';
                            if (card) {
                                const t = card.querySelector('h2, h3, h4, [class*="title"], [class*="name"]');
                                if (t) text = t.textContent.trim();
                            }
                            if (!text) text = a.textContent.trim();
                            if (!text) {
                                const img = a.querySelector('img');
                                if (img) text = img.alt || '';
                            }
                            if (text && text.length > 2 && text !== 'Anime') {
                                seen.add(href);
                                links.push({name: text.substring(0, 80), url: href});
                            }
                        });
                        return links;
                    }
                """)

                new_count = 0
                for l in anime_links:
                    if l["url"] not in seen_urls:
                        seen_urls.add(l["url"])
                        all_links.append(l)
                        new_count += 1

                print(f"         {new_count} novos (total: {len(all_links)})")

                if max_animes > 0 and len(all_links) >= max_animes:
                    break
                if new_count == 0:
                    break
                await asyncio.sleep(1)

            if max_animes > 0:
                all_links = all_links[:max_animes]

            total_encontrados = len(all_links)
            print(f"\n  [INFO] Scrapando {total_encontrados} animes...")
            for i, link in enumerate(all_links):
                print(f"\n  {i+1}/{total_encontrados}")
                try:
                    await _scrape_single_anime_pw(page, context, link['url'], site_domain, test, force, max_eps)
                except Exception as e:
                    print(f"    [ERRO] {e}")
                await asyncio.sleep(2)

            await browser.close()
            return

        # Anime específico
        await _scrape_single_anime_pw(page, context, anime_url, site_domain, test, force, max_eps)
        await browser.close()


def run_pw_site(anime_url, site_name, site_domain, max_eps=0, test=True, force=False, scrape_all=False, max_animes=0):
    return asyncio.run(_scrape_pw_site(anime_url, site_name, site_domain, test, force, max_eps, scrape_all, max_animes))


def scrape_all_supported_sites(max_animes=0, max_eps=0):
    """Scrapeia cada catálogo suportado e mescla tudo no mesmo JSON."""
    print(f"\n[CATÁLOGO GERAL] Limite por site: {max_animes or 'todos'}; episódios por anime: {max_eps or 'todos'}")

    print("\n===== animesonlinecc.to =====")
    scrape_entire_animesonlinecc(max_animes=max_animes, max_eps=max_eps, test=False, force=False)

    for site_name, site_domain in (("animefire.one", "animefire.one"), ("anizero.org", "anizero.org")):
        if not HAS_PLAYWRIGHT:
            print(f"\n[AVISO] Pulando {site_domain}: Playwright não está instalado.")
            continue
        print(f"\n===== {site_domain} =====")
        run_pw_site("", site_name, site_domain, max_eps=max_eps, test=False,
                    force=False, scrape_all=True, max_animes=max_animes)

    print("\n===== animesonline.cloud =====")
    scrape_animesonlinecloud(max_animes=max_animes)

    print("\n===== animesonlines.net =====")
    scrape_animesonlines_net(max_animes=max_animes)

    show_status()


# ═══════════════════════════════════════════════════════════
# MENU
# ═══════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("  ANIME SCRAPER - Multi-site (v10)")
    print(f"  Output: {OUTPUT_FILE}")
    if not HAS_PLAYWRIGHT:
        print("  [AVISO] Playwright não instalado (sites 2,3 não funcionam)")
        print("  Instale: pip install playwright && playwright install chromium")
    print("=" * 60)
    print("\n  [1] animesonlinecc.to")
    print("  [2] animefire.one")
    print("  [3] anizero.org")
    print("  [4] animesonline.cloud (legendados)")
    print("  [5] animesonlines.net")
    print("  [6] SCRAPEAR TODOS OS SITES")
    print("  [7] Ver status")
    site = input("\n  Escolha (1-7): ").strip()

    if site == "7":
        show_status()
        return

    if site == "6":
        max_animes = int(input("  Quantos animes por site? (0=todos): ").strip() or "0")
        max_eps = int(input("  Máx episódios por anime? (0=todos): ").strip() or "0")
        scrape_all_supported_sites(max_animes=max_animes, max_eps=max_eps)
        return

    if site == "1":
        print("\n  [1] Anime específico")
        print("  [2] Teste rápido (3 eps)")
        print("  [3] Scrape TODO o site")
        mode = input("  Modo (1-3): ").strip()

        if mode == "3":
            max_animes = int(input("  Quantos animes? (Enter=1, 0=todos): ").strip() or "1")
            max_eps = int(input("  Eps por anime (0=todos): ").strip() or "0")
            test = input("  Testar? (s/n): ").strip().lower() == 's'
            force = input("  Forçar? (s/n): ").strip().lower() == 's'
            scrape_entire_animesonlinecc(max_animes, max_eps, test, force)
            return

        max_eps = 3 if mode == "2" else 0
        anime_url = input("  URL do anime: ").strip()
        if not anime_url:
            print("  [ERRO] URL vazia.")
            return
        force = input("  Forçar? (s/n): ").strip().lower() == 's'
        scrape_animesonlinecc(anime_url, max_eps, test=True, force=force)

    elif site == "4":
        max_animes = int(input("  Quantos títulos? (Enter ou 0 = todos): ").strip() or "0")
        scrape_animesonlinecloud(max_animes)
        return

    elif site == "5":
        max_animes = int(input("  Quantos itens? (Enter ou 0 = todos): ").strip() or "0")
        scrape_animesonlines_net(max_animes)
        return

    elif site in ("2", "3"):
        if not HAS_PLAYWRIGHT:
            print("\n  [ERRO] Playwright não instalado!")
            return

        site_names = {"2": ("animefire.one", "animefire.one"), "3": ("anizero.org", "anizero.org")}
        site_name, site_domain = site_names[site]

        print(f"\n  [1] Anime específico")
        print(f"  [2] Scrape TODO o site ({site_domain})")
        mode = input("  Modo (1-2): ").strip()

        if mode == "2":
            max_animes = int(input("  Quantos animes? (Enter=1, 0=todos): ").strip() or "1")
            max_eps = int(input("  Máx eps por anime (0=todos): ").strip() or "0")
            test = input("  Testar? (s/n): ").strip().lower() == 's'
            force = input("  Forçar? (s/n): ").strip().lower() == 's'
            run_pw_site("", site_name, site_domain, max_eps, test, force, scrape_all=True, max_animes=max_animes)
            return

        anime_url = input("  URL do anime: ").strip()
        if not anime_url:
            print("  [ERRO] URL vazia.")
            return
        max_eps = int(input("  Máx eps (0=todos): ").strip() or "0")
        test = input("  Testar? (s/n): ").strip().lower() == 's'
        force = input("  Forçar? (s/n): ").strip().lower() == 's'
        run_pw_site(anime_url, site_name, site_domain, max_eps, test, force)

    else:
        print("  [ERRO] Opção inválida.")
        return

    print(f"\n  python -m http.server 8080 → http://localhost:8080/index.html")


if __name__ == "__main__":
    main()
