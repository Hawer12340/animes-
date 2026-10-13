import json
import re
import sys
import io
import os
import msvcrt
import time
from collections import defaultdict
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": UA}
CDN_BASE = "https://cdn-sv01.maximaimg.online/stream"
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "animes.json")
REPORT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "update_report.json")


def _serialize_database_access(func):
    def wrapped(*args, **kwargs):
        with open(OUTPUT_FILE + ".lock", "a+b") as lock_file:
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


def load_animes() -> list:
    with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_animes(animes: list):
    temp_path = f"{OUTPUT_FILE}.{os.getpid()}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(animes, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, OUTPUT_FILE)


def build_stream_urls(slug: str, ep: int, version: str = "dublado") -> list:
    prefix = "b" if version == "dublado" else "e"
    path = f"/{prefix}/{slug}/{ep:02d}.mp4/index.m3u8"
    mirrors = [
        "https://cdn-sv01.maximaimg.online/stream",
        "https://cdn-sv02.maximaimg.online/stream",
        "https://cdn-sv03.maximaimg.online/stream",
    ]
    return [f"{m}{path}" for m in mirrors]


def test_url(url: str, timeout: int = 8) -> bool:
    try:
        r = requests.head(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        return r.status_code == 200
    except Exception:
        return False


def _clean_title(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "Desconhecido")).strip()
    value = re.sub(r"\s*(?:\[|\(|-|\|)?\s*(?:dublado|legendado|dub|sub)\s*(?:\]|\))?\s*$", "", value, flags=re.I)
    return value.strip() or "Desconhecido"


def _url_values(value):
    if isinstance(value, str):
        return [value] if value.startswith(("http://", "https://")) else []
    if isinstance(value, dict):
        return _url_values(value.get("url") or value.get("src"))
    if isinstance(value, list):
        return [url for item in value for url in _url_values(item)]
    return []


def _merge_duplicate_records(records):
    """Une entradas do mesmo título e versão sem perder fontes nem temporadas."""
    grouped = {}
    changed = False
    for original in records:
        if not isinstance(original, dict):
            changed = True
            continue
        anime = dict(original)
        anime["title"] = _clean_title(anime.get("title"))
        version = str(anime.get("version") or "indefinido").strip().lower()
        version = {"dub": "dublado", "sub": "legendado", "leg": "legendado"}.get(version, version)
        anime["version"] = version
        key = (anime["title"].casefold(), version)
        if key not in grouped:
            grouped[key] = anime
            continue
        changed = True
        base = grouped[key]
        for field in ("image", "synopsis", "genres", "info", "seasons_count"):
            if not base.get(field) and anime.get(field):
                base[field] = anime[field]
        sites = []
        for name in (base.get("sites", []) or []) + [base.get("site")] + (anime.get("sites", []) or []) + [anime.get("site")]:
            if name and name not in sites:
                sites.append(name)
        if sites:
            base["sites"] = sites
        urls = []
        for url in (base.get("source_urls", []) or []) + [base.get("source_url"), anime.get("source_url")] + (anime.get("source_urls", []) or []):
            if url and url not in urls:
                urls.append(url)
        if urls:
            base["source_url"], base["source_urls"] = urls[0], urls
        episodes = {}
        for ep in (base.get("episodes", []) or []) + (anime.get("episodes", []) or []):
            if not isinstance(ep, dict):
                continue
            ep = dict(ep)
            try:
                season, number = int(ep.get("season", 1) or 1), int(ep.get("number", 0) or 0)
            except (TypeError, ValueError):
                continue
            if number <= 0:
                continue
            ep["season"], ep["number"], ep["version"] = season, number, version
            k = (season, number)
            if k not in episodes:
                episodes[k] = ep
            else:
                old = episodes[k]
                for field in ("sources", "players"):
                    combined, seen = [], set()
                    for item in (old.get(field, []) or []) + (ep.get(field, []) or []):
                        urls = _url_values(item)
                        marker = urls[0] if urls else str(item)
                        if marker and marker not in seen:
                            seen.add(marker); combined.append(item)
                    if combined:
                        old[field] = combined
                for field in ("episode_url", "url", "name"):
                    if not old.get(field) and ep.get(field):
                        old[field] = ep[field]
        base["episodes"] = sorted(episodes.values(), key=lambda ep: (ep["season"], ep["number"]))
        base["total_episodes"] = len(base["episodes"])
    return list(grouped.values()), changed


def _check_link(url: str, timeout: int = 10):
    """Retorna (ok, motivo); aceita endpoints que bloqueiam HEAD e respondem a GET."""
    try:
        response = requests.head(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if response.status_code in (405, 403, 501):
            response.close()
            response = requests.get(url, headers={**HEADERS, "Range": "bytes=0-0"}, timeout=timeout, allow_redirects=True, stream=True)
        status = response.status_code
        response.close()
        return status < 400, f"HTTP {status}"
    except Exception as exc:
        return False, str(exc)[:180]


def audit_database(animes):
    """Valida capas, players/fontes e estrutura, registrando problemas sem apagar dados."""
    report = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "anime_count": len(animes), "checked_links": 0, "issues": []}
    cache = {}
    for anime in animes:
        if not isinstance(anime, dict):
            report["issues"].append({"title": "?", "type": "registro_invalido", "detail": "Registro não é um objeto"})
            continue
        title = anime.get("title", "Desconhecido")
        image_url = anime.get("image")
        if not image_url:
            report["issues"].append({"title": title, "type": "capa_ausente"})
        else:
            urls = _url_values(image_url)
            for url in urls[:1]:
                if url not in cache:
                    cache[url] = _check_link(url)
                    report["checked_links"] += 1
                ok, reason = cache[url]
                if not ok:
                    report["issues"].append({"title": title, "type": "capa_com_erro", "url": url, "detail": reason})
        eps = anime.get("episodes", []) or []
        seen = set()
        for ep in eps:
            if not isinstance(ep, dict):
                report["issues"].append({"title": title, "type": "episodio_invalido"}); continue
            key = (ep.get("season", 1), ep.get("number"), ep.get("version", anime.get("version", "")))
            if key in seen:
                report["issues"].append({"title": title, "type": "episodio_duplicado", "episode": list(key)})
            seen.add(key)
            candidates = _url_values(ep.get("sources")) + _url_values(ep.get("players"))
            if not candidates:
                candidates = _url_values(ep.get("url")) + _url_values(ep.get("episode_url"))
            if not candidates:
                report["issues"].append({"title": title, "type": "player_ausente", "season": key[0], "episode": key[1]})
            for url in dict.fromkeys(candidates):
                if url not in cache:
                    cache[url] = _check_link(url)
                    report["checked_links"] += 1
                ok, reason = cache[url]
                if not ok:
                    report["issues"].append({"title": title, "type": "player_com_erro", "season": key[0], "episode": key[1], "url": url, "detail": reason})
    report["issue_count"] = len(report["issues"])
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


def update_animesdigital(anime: dict) -> int:
    """
    Verifica se há episódios novos e adiciona.
    Retorna quantos episódios novos foram adicionados.
    """
    added = 0
    episodes = anime.get("episodes", [])

    # Agrupa por temporada + versão
    # Ex: {1: {"dublado": 12, "legendado": 12}, 2: {"dublado": 5, "legendado": 5}}
    max_per_group = {}
    for ep in episodes:
        season = ep.get("season", 1)
        version = ep.get("version", "legendado")
        key = (season, version)
        if key not in max_per_group:
            max_per_group[key] = 0
        max_per_group[key] = max(max_per_group[key], ep.get("number", 0))

    # Pega os slugs (do primeiro episódio de cada grupo)
    slugs = {}
    for ep in episodes:
        season = ep.get("season", 1)
        version = ep.get("version", "legendado")
        key = (season, version)
        if key not in slugs and ep.get("sources"):
            # Extrai slug da URL
            # URL: https://cdn-sv01.maximaimg.online/stream/b/bleach-dublado/01.mp4/index.m3u8
            m = re.search(r'/stream/[be]/([^/]+)/', ep["sources"][0])
            if m:
                slugs[key] = m.group(1)

    # Tenta o próximo episódio de cada grupo
    for (season, version), max_ep in max_per_group.items():
        slug = slugs.get((season, version))
        if not slug:
            continue

        # Tenta até 5 episódios à frente
        for next_ep in range(max_ep + 1, max_ep + 6):
            urls = build_stream_urls(slug, next_ep, version)
            if test_url(urls[0]):
                # Episódio novo!
                print(f"    + Ep {next_ep} T{season} ({version})")
                anime["episodes"].append({
                    "number": next_ep,
                    "season": season,
                    "version": version,
                    "sources": urls,
                    "test": None,
                    "ok": True,
                })
                anime["total_episodes"] = len(anime["episodes"])
                added += 1
            else:
                break  # Se o próximo não existe, para

    return added


def update_animesonlinecc(anime: dict) -> int:
    """Verifica episódios novos no animesonlinecc."""
    added = 0
    episodes = anime.get("episodes", [])
    if not episodes:
        return 0

    max_ep = max(e.get("number", 0) for e in episodes)
    source_url = anime.get("source_url", "")

    # Re-scrapa a página do anime pra ver se tem mais episódios
    try:
        r = requests.get(source_url, headers=HEADERS, timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")

        new_eps = []
        for a in soup.select('a[href*="/episodio/"]'):
            url = a["href"]
            if url.startswith("/"):
                url = "https://animesonlinecc.to" + url
            m = re.search(r'episodio-(\d+)', url)
            num = int(m.group(1)) if m else 0
            if num > max_ep and num > 0:
                name = a.get_text(strip=True)
                version = "dublado" if "dub" in name.lower() else "legendado"
                new_eps.append({"number": num, "url": url, "name": name, "version": version})

        new_eps.sort(key=lambda e: e["number"])

        for ep in new_eps:
            print(f"    + Ep {ep['number']} ({ep['version']})")
            # Tenta pegar o player
            try:
                ep_soup = BeautifulSoup(requests.get(ep["url"], headers=HEADERS, timeout=30).text, "html.parser")
                players = []
                for iframe in ep_soup.find_all("iframe"):
                    src = iframe.get("src", "")
                    if src and src.startswith("http"):
                        players.append({"url": src, "type": "iframe"})
                for m in re.finditer(r'(https?://[^\s"\'<>]+\.(?:m3u8|mp4))', str(ep_soup)):
                    players.append({"url": m.group(1), "type": "regex"})

                anime["episodes"].append({**ep, "players": players, "player_count": len(players), "season": 1})
                added += 1
            except Exception:
                anime["episodes"].append({**ep, "players": [], "player_count": 0, "season": 1})
                added += 1

        if added:
            anime["total_episodes"] = len(anime["episodes"])

    except Exception as e:
        print(f"    [ERRO] {e}")

    return added


def update_animefire(anime: dict) -> int:
    """
    Para animefire, precisa Playwright.
    Verifica se a quantidade de episódios aumentou.
    """
    # Placeholder — animefire é SPA, precisa Playwright
    # Por enquanto só avisa
    print("    [INFO] Animefire: rode o scraper manualmente para atualizar (SPA)")
    return 0


@_serialize_database_access
def run_update():
    print("=" * 60)
    print("  ANIME UPDATER - Verifica episódios novos")
    print("=" * 60)

    animes = load_animes()
    print(f"\n  Animes no JSON: {len(animes)}")
    print(f"  Verificando...\n")

    total_added = 0

    interrupted = False
    try:
      for i, anime in enumerate(animes):
        site = anime.get("site", "")
        title = anime.get("title", "Desconhecido")
        current_eps = anime.get("total_episodes", 0)

        print(f"  [{i+1}/{len(animes)}] {title} ({site}) - {current_eps} eps", end=" ")

        if site == "animesdigital.org":
            added = update_animesdigital(anime)
        elif site == "animesonlinecc.to":
            added = update_animesonlinecc(anime)
        elif site == "animefire.one":
            added = update_animefire(anime)
        else:
            print("→ desconhecido")
            continue

        if added > 0:
            total_added += added
            print(f"→ +{added} novos!")
        else:
            print("→ atualizado")

        time.sleep(0.5)
    except KeyboardInterrupt:
        interrupted = True
        print("\n  [INFO] Interrompido pelo usuário; salvando o progresso parcial...")

    # Consolida registros e episódios repetidos mantendo todas as fontes.
    animes, deduplicated = _merge_duplicate_records(animes)
    if deduplicated:
        print("\n  Duplicatas de título/versão consolidadas.")

    # Salva antes da auditoria, que pode demorar por depender de servidores externos.
    save_animes(animes)

    if interrupted:
        print("  [OK] Progresso salvo. Auditoria não executada.")
        return

    # Audita capa e links dos episódios; erros ficam detalhados no relatório.
    print("\n  Validando capas e players (pode levar alguns minutos)...")
    try:
        report = audit_database(animes)
    except KeyboardInterrupt:
        print("\n  [INFO] Auditoria interrompida pelo usuário. Banco já está salvo.")
        return

    print(f"\n{'='*60}")
    if total_added > 0:
        print(f"  [OK] {total_added} episódio(s) novo(s) adicionados!")
    else:
        print(f"  [OK] Tudo em dia. Nenhum episódio novo.")
    print(f"  Auditoria: {report['checked_links']} links verificados; {report['issue_count']} problema(s).")
    print(f"  Relatório: {REPORT_FILE}")
    print(f"{'='*60}")


if __name__ == "__main__":
    run_update()   
