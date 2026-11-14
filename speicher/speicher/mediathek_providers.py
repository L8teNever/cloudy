"""Online-Metadaten für Filme, Serien & Anime (TVmaze, AniList, TMDB) – angelehnt an NINA.

Liefert einheitliche Treffer- und Detailobjekte inkl. deutscher Beschreibungen,
Staffeln/Folgen und deutscher Streaming-Anbieter (watch/providers DE).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import data_dir

TIMEOUT = 10
IMAGE_HOSTS = {
    "aniworld.to",
    "image.tmdb.org",
    "media.themoviedb.org",
    "static.tvmaze.com",
    "s4.anilist.co",
    "img.youtube.com",
    "i.ytimg.com",
}
_AL_NODE_CACHE: dict[int, dict] = {}
_AL_LOCK = threading.Lock()
_CACHE_DIR_MAX_BYTES = 150 * 1024 * 1024


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", s)


def sanitize_name(name: str, fallback: str = "Unbenannt") -> str:
    """Macht einen Titel sicher für Ordner- und Dateinamen (ohne / \\ : * ? \" < > |)."""
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", str(name or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:120] or fallback


def parse_episode_from_filename(filename: str) -> tuple[int | None, int | None]:
    """Erkennt Staffel und Folge aus Dateinamen wie 'S01E02', '1x02', 'Staffel 2 Folge 5', 'E03', '04.mkv'."""
    stem = Path(filename).stem
    for pat in (
        r"[Ss](\d{1,2})[\s._-]*[Ee](\d{1,3})",
        r"(\d{1,2})x(\d{1,3})",
        r"[Ss]taffel\s*(\d{1,2})\D+[Ff]olge\s*(\d{1,3})",
    ):
        m = re.search(pat, stem)
        if m:
            return int(m.group(1)), int(m.group(2))
    for pat in (
        r"(?:^|[\s._-])(?:[Ee][Pp]?|[Ff]olge|[Ee]pisode)\s*0*(\d{1,3})(?:$|[\s._-])",
        r"(?:^|[\s._-])(\d{1,3})(?:$|[\s._-])",
    ):
        m = re.search(pat, stem)
        if m:
            num = int(m.group(1))
            if 1 <= num <= 999:
                return None, num
    return None, None


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", str(html or ""))
    return re.sub(r"\s+", " ", text).strip()


def _get_json(url: str, headers: dict[str, str] | None = None, body: bytes | None = None) -> dict | list:
    hdrs = {"User-Agent": "cloudy-speicher/1.0", "Accept": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST" if body is not None else "GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RuntimeError("Zu viele Anfragen – kurz warten") from exc
        if exc.code == 401:
            raise RuntimeError("TMDB-Schlüssel ungültig") from exc
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Netzwerkfehler: {exc.reason}") from exc


# ---------- TMDB ----------

def _tmdb_fetch(path: str, tmdb_key: str, params: dict | None = None) -> dict:
    key = (tmdb_key or "").strip()
    if not key:
        raise RuntimeError("Kein TMDB-Schlüssel eingetragen")
    q = {"language": "de-DE", **(params or {})}
    headers: dict[str, str] = {}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    else:
        q["api_key"] = key
    url = "https://api.themoviedb.org/3" + path + "?" + urllib.parse.urlencode(q)
    data = _get_json(url, headers=headers)
    return data if isinstance(data, dict) else {}


def _tmdb_img(path: str | None, size: str = "w500") -> str:
    return f"https://image.tmdb.org/t/p/{size}{path}" if path else ""


def _tmdb_pop(t: dict) -> float:
    votes = math.log10((t.get("vote_count") or 0) + 1) / 4.5
    pop = math.log10((t.get("popularity") or 0) + 1) / 3.0
    return min(1.0, max(votes, pop))


def _tmdb_anime(t: dict) -> bool:
    gids = t.get("genre_ids") or [g.get("id") for g in (t.get("genres") or [])]
    return 16 in gids and (t.get("original_language") == "ja" or "JP" in (t.get("origin_country") or []))


def _from_tmdb_tv(t: dict) -> dict:
    seasons = [
        {"n": s["season_number"], "eps": s.get("episode_count") or 0}
        for s in (t.get("seasons") or [])
        if (s.get("season_number") or 0) > 0
    ]
    fad = t.get("first_air_date") or ""
    year = int(fad[:4]) if len(fad) >= 4 and fad[:4].isdigit() else None
    nets = t.get("networks") or []
    runtimes = t.get("episode_run_time") or []
    return {
        "ref": f"tmdb:{t['id']}",
        "provider": "tmdb",
        "type": "show",
        "id": t["id"],
        "name": t.get("name") or t.get("original_name") or "",
        "altNames": [x for x in (t.get("original_name"),) if x],
        "year": year,
        "poster": _tmdb_img(t.get("poster_path"), "w500"),
        "backdrop": _tmdb_img(t.get("backdrop_path"), "w1280"),
        "network": (nets[0].get("name") if nets else "") or "",
        "genres": [g.get("name", "") for g in (t.get("genres") or []) if g.get("name")],
        "status": t.get("status") or "",
        "summary": t.get("overview") or "",
        "score": round(t["vote_average"], 1) if t.get("vote_average") else None,
        "totalEpisodes": t.get("number_of_episodes") or None,
        "seasons": seasons,
        "runtime": runtimes[0] if runtimes else None,
        "url": f"https://www.themoviedb.org/tv/{t['id']}",
        "pop": _tmdb_pop(t),
        "anime": _tmdb_anime(t),
    }


def _from_tmdb_movie(t: dict) -> dict:
    rd = t.get("release_date") or ""
    year = int(rd[:4]) if len(rd) >= 4 and rd[:4].isdigit() else None
    return {
        "ref": f"tmdbmovie:{t['id']}",
        "provider": "tmdbmovie",
        "type": "movie",
        "id": t["id"],
        "name": t.get("title") or t.get("original_title") or "",
        "altNames": [x for x in (t.get("original_title"),) if x],
        "year": year,
        "poster": _tmdb_img(t.get("poster_path"), "w500"),
        "backdrop": _tmdb_img(t.get("backdrop_path"), "w1280"),
        "network": "Film",
        "genres": [g.get("name", "") for g in (t.get("genres") or []) if g.get("name")],
        "status": t.get("status") or "",
        "summary": t.get("overview") or "",
        "score": round(t["vote_average"], 1) if t.get("vote_average") else None,
        "totalEpisodes": 1,
        "seasons": [],
        "runtime": t.get("runtime") or None,
        "url": f"https://www.themoviedb.org/movie/{t['id']}",
        "pop": _tmdb_pop(t),
        "anime": _tmdb_anime(t),
    }


def _tmdb_watch_de(wp: dict) -> dict:
    de = ((wp or {}).get("results") or {}).get("DE") or {}
    out: list[dict] = []
    seen: set[str] = set()
    for key, label in (("flatrate", "Streamen"), ("free", "Kostenlos"), ("ads", "Mit Werbung"), ("rent", "Leihen"), ("buy", "Kaufen")):
        for p in de.get(key) or []:
            pname = p.get("provider_name") or ""
            if not pname or pname in seen:
                continue
            seen.add(pname)
            out.append({
                "name": pname,
                "type": label,
                "logo": _tmdb_img(p.get("logo_path"), "w92"),
                "url": de.get("link") or "",
            })
    return {"link": de.get("link") or "", "list": out}


def _full_tmdb_tv(tmdb_id: str, tmdb_key: str) -> dict:
    t = _tmdb_fetch(f"/tv/{tmdb_id}", tmdb_key, {"append_to_response": "aggregate_credits,videos,watch/providers,external_ids"})
    meta = _from_tmdb_tv(t)
    watch = _tmdb_watch_de(t.get("watch/providers") or {})
    videos = (t.get("videos") or {}).get("results") or []
    yt = next((v for v in videos if v.get("site") == "YouTube" and v.get("type") == "Trailer"), None) or next(
        (v for v in videos if v.get("site") == "YouTube"), None
    )
    cast = [
        {
            "name": c.get("name") or "",
            "role": (c.get("roles") or [{}])[0].get("character", ""),
            "image": _tmdb_img(c.get("profile_path"), "w185"),
        }
        for c in ((t.get("aggregate_credits") or {}).get("cast") or [])[:16]
        if c.get("name")
    ]
    return {
        **meta,
        "watch": watch,
        "ratings": [{"label": "TMDB", "value": f"{round(t['vote_average'] * 10)} %", "url": meta["url"]}] if t.get("vote_average") else [],
        "trailer": f"https://www.youtube.com/watch?v={yt['key']}" if yt else "",
        "cast": cast,
        "episodes": [],
    }


def _full_tmdb_movie(tmdb_id: str, tmdb_key: str) -> dict:
    t = _tmdb_fetch(f"/movie/{tmdb_id}", tmdb_key, {"append_to_response": "credits,videos,watch/providers"})
    meta = _from_tmdb_movie(t)
    watch = _tmdb_watch_de(t.get("watch/providers") or {})
    videos = (t.get("videos") or {}).get("results") or []
    yt = next((v for v in videos if v.get("site") == "YouTube" and v.get("type") == "Trailer"), None) or next(
        (v for v in videos if v.get("site") == "YouTube"), None
    )
    cast = [
        {"name": c.get("name") or "", "role": c.get("character") or "", "image": _tmdb_img(c.get("profile_path"), "w185")}
        for c in ((t.get("credits") or {}).get("cast") or [])[:16]
        if c.get("name")
    ]
    return {
        **meta,
        "watch": watch,
        "ratings": [{"label": "TMDB", "value": f"{round(t['vote_average'] * 10)} %", "url": meta["url"]}] if t.get("vote_average") else [],
        "trailer": f"https://www.youtube.com/watch?v={yt['key']}" if yt else "",
        "cast": cast,
        "episodes": [],
    }


def _tmdb_season(tmdb_id: str, season: int, tmdb_key: str) -> list[dict]:
    s = _tmdb_fetch(f"/tv/{tmdb_id}/season/{season}", tmdb_key)
    return [
        {
            "season": season,
            "number": e.get("episode_number"),
            "title": e.get("name") or f"Folge {e.get('episode_number')}",
            "image": _tmdb_img(e.get("still_path"), "w300"),
            "runtime": e.get("runtime"),
            "airdate": e.get("air_date") or "",
        }
        for e in (s.get("episodes") or [])
        if e.get("episode_number")
    ]


# ---------- TVmaze ----------

def _from_tvmaze(s: dict) -> dict:
    emb = s.get("_embedded") or {}
    seasons = [
        {"n": x["number"], "eps": x.get("episodeOrder") or 0}
        for x in (emb.get("seasons") or [])
        if (x.get("number") or 0) > 0
    ]
    prem = s.get("premiered") or ""
    year = int(prem[:4]) if len(prem) >= 4 and prem[:4].isdigit() else None
    img = s.get("image") or {}
    net = s.get("network") or s.get("webChannel") or {}
    rating = (s.get("rating") or {}).get("average")
    eps_emb = [e for e in (emb.get("episodes") or []) if (e.get("season") or 0) > 0]
    total = len(eps_emb) if eps_emb else (sum(x["eps"] for x in seasons) or None)
    return {
        "ref": f"tvmaze:{s['id']}",
        "provider": "tvmaze",
        "type": "show",
        "id": s["id"],
        "name": s.get("name") or "",
        "altNames": [],
        "year": year,
        "poster": img.get("original") or img.get("medium") or "",
        "backdrop": img.get("original") or "",
        "network": net.get("name") or "",
        "genres": s.get("genres") or [],
        "status": s.get("status") or "",
        "summary": _strip_html(s.get("summary") or ""),
        "score": round(float(rating), 1) if rating else None,
        "totalEpisodes": total,
        "seasons": seasons,
        "runtime": s.get("runtime") or s.get("averageRuntime"),
        "url": s.get("url") or "",
        "pop": min(1.0, (s.get("weight") or 0) / 100.0 * 0.6),
        "anime": "Anime" in (s.get("genres") or []),
    }


def _full_tvmaze(tvm_id: str, tmdb_key: str = "") -> dict:
    s = _get_json(f"https://api.tvmaze.com/shows/{urllib.parse.quote(tvm_id)}?embed[]=seasons&embed[]=episodes&embed[]=cast")
    if not isinstance(s, dict):
        raise RuntimeError("Ungültige TVmaze-Antwort")
    meta = _from_tvmaze(s)
    emb = s.get("_embedded") or {}
    eps = [
        {
            "season": e["season"],
            "number": e["number"],
            "title": e.get("name") or f"Folge {e['number']}",
            "image": (e.get("image") or {}).get("medium") or "",
            "runtime": e.get("runtime") or meta.get("runtime"),
            "airdate": e.get("airdate") or "",
        }
        for e in (emb.get("episodes") or [])
        if (e.get("season") or 0) > 0 and e.get("number")
    ]
    if not meta["seasons"] and eps:
        counts: dict[int, int] = {}
        for ep in eps:
            counts[ep["season"]] = counts.get(ep["season"], 0) + 1
        meta["seasons"] = [{"n": k, "eps": v} for k, v in sorted(counts.items())]
    cast = [
        {
            "name": (c.get("person") or {}).get("name") or (c.get("character") or {}).get("name") or "",
            "role": (c.get("character") or {}).get("name") or "",
            "image": ((c.get("character") or {}).get("image") or {}).get("medium")
            or ((c.get("person") or {}).get("image") or {}).get("medium")
            or "",
        }
        for c in (emb.get("cast") or [])[:16]
        if (c.get("person") or {}).get("name") or (c.get("character") or {}).get("name")
    ]
    net = s.get("webChannel") or s.get("network")
    watch = {
        "link": "",
        "list": [{"name": net["name"], "type": "Streamen" if s.get("webChannel") else "Sender", "logo": "", "url": s.get("officialSite") or ""}]
        if net and net.get("name")
        else [],
    }
    ext = s.get("externals") or {}
    ratings = []
    if meta.get("score"):
        ratings.append({"label": "TVmaze", "value": f"{meta['score']:.1f}", "url": meta["url"]})
    if ext.get("imdb"):
        ratings.append({"label": "IMDb", "value": "↗", "url": f"https://www.imdb.com/title/{ext['imdb']}"})
    trailer = ""
    if tmdb_key and (ext.get("imdb") or ext.get("thetvdb")):
        try:
            f = (
                _tmdb_fetch(f"/find/{ext['imdb']}", tmdb_key, {"external_source": "imdb_id"})
                if ext.get("imdb")
                else _tmdb_fetch(f"/find/{ext['thetvdb']}", tmdb_key, {"external_source": "tvdb_id"})
            )
            hit = (f.get("tv_results") or [None])[0]
            if hit:
                t = _full_tmdb_tv(str(hit["id"]), tmdb_key)
                if (t.get("watch") or {}).get("list"):
                    watch = t["watch"]
                if t.get("summary"):
                    meta["summary"] = t["summary"]
                if t.get("backdrop"):
                    meta["backdrop"] = t["backdrop"]
                if t.get("trailer"):
                    trailer = t["trailer"]
                ratings.extend(t.get("ratings") or [])
        except Exception:  # noqa: BLE001
            pass
    return {**meta, "watch": watch, "ratings": ratings, "trailer": trailer, "cast": cast, "episodes": eps}


# ---------- AniList ----------

_AL_FIELDS = """
id format duration status episodes averageScore popularity genres siteUrl description(asHtml: false)
title { romaji english userPreferred } coverImage { large extraLarge } startDate { year }
bannerImage trailer { id site }
studios(isMain: true) { nodes { name } }
"""


def _anilist_query(query: str, variables: dict) -> dict:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    r = _get_json(
        "https://graphql.anilist.co",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        body=payload,
    )
    if not isinstance(r, dict):
        raise RuntimeError("Ungültige AniList-Antwort")
    if r.get("errors"):
        raise RuntimeError(r["errors"][0].get("message") or "AniList-Fehler")
    return r.get("data") or {}


def _from_anilist(m: dict) -> dict:
    title_obj = m.get("title") or {}
    name = title_obj.get("english") or title_obj.get("userPreferred") or title_obj.get("romaji") or ""
    alt_names = [x for x in (title_obj.get("romaji"), title_obj.get("english"), title_obj.get("userPreferred")) if x]
    eps = m.get("episodes")
    cov = m.get("coverImage") or {}
    studios = ((m.get("studios") or {}).get("nodes") or [])
    avg = m.get("averageScore")
    return {
        "ref": f"anilist:{m['id']}",
        "provider": "anilist",
        "type": "movie" if m.get("format") == "MOVIE" else "show",
        "id": m["id"],
        "name": name,
        "altNames": alt_names,
        "year": (m.get("startDate") or {}).get("year"),
        "poster": cov.get("large") or cov.get("extraLarge") or "",
        "backdrop": m.get("bannerImage") or cov.get("extraLarge") or cov.get("large") or "",
        "network": (studios[0].get("name") if studios else "") or "Anime",
        "genres": m.get("genres") or [],
        "status": m.get("status") or "",
        "summary": _strip_html(m.get("description") or ""),
        "score": round(avg / 10.0, 1) if avg else None,
        "totalEpisodes": eps or None,
        "seasons": [{"n": 1, "eps": eps}] if eps and m.get("format") != "MOVIE" else [],
        "runtime": m.get("duration"),
        "url": m.get("siteUrl") or "",
        "pop": min(1.0, math.log10((m.get("popularity") or 0) + 1) / 6.0),
        "anime": True,
    }


def _al_node(al_id: int) -> dict:
    with _AL_LOCK:
        cached = _AL_NODE_CACHE.get(al_id)
    if cached:
        return cached
    q = """query ($id: Int) { Media(id: $id, type: ANIME) { id format episodes duration
      streamingEpisodes { title thumbnail url site }
      relations { edges { relationType node { id format } } } } }"""
    node = _anilist_query(q, {"id": al_id}).get("Media") or {}
    with _AL_LOCK:
        if len(_AL_NODE_CACHE) > 500:
            _AL_NODE_CACHE.clear()
        _AL_NODE_CACHE[al_id] = node
    return node


def _al_seasons(al_id: int) -> list[dict]:
    start = _al_node(al_id)
    seen = {start["id"]}
    root = start

    def pick(node: dict, rel: str, visited: set[int]) -> dict | None:
        edges = [
            e
            for e in ((node.get("relations") or {}).get("edges") or [])
            if e.get("relationType") == rel and e.get("node") and e["node"].get("format") != "MUSIC" and e["node"]["id"] not in visited
        ]
        return next((e for e in edges if e["node"].get("format") in ("TV", "ONA")), edges[0] if edges else None)

    for _ in range(25):
        p = pick(root, "PREQUEL", seen)
        if not p:
            break
        seen.add(p["node"]["id"])
        root = _al_node(p["node"]["id"])
    chain = [root]
    done = {root["id"]}
    cur = root
    for _ in range(25):
        s = pick(cur, "SEQUEL", done)
        if not s:
            break
        done.add(s["node"]["id"])
        cur = _al_node(s["node"]["id"])
        chain.append(cur)
    return [m for m in chain if m["id"] == start["id"] or (m.get("format") in ("TV", "ONA") and (m.get("episodes") or 0) > 0)]


def _full_anilist(al_id_str: str) -> dict:
    al_id = int(al_id_str)
    q = f"""query ($id: Int) {{ Media(id: $id, type: ANIME) {{
      {_AL_FIELDS}
      idMal
      streamingEpisodes {{ title thumbnail url site }}
      externalLinks {{ site url type icon color }}
      characters(perPage: 16, sort: [ROLE, RELEVANCE]) {{
        edges {{ role node {{ name {{ full }} image {{ medium }} }} voiceActors(language: JAPANESE) {{ name {{ full }} }} }}
      }}
    }} }}"""
    m = _anilist_query(q, {"id": al_id}).get("Media") or {}
    meta = _from_anilist(m)

    def season_eps(media: dict, season_num: int) -> list[dict]:
        by_num: dict[int, dict] = {}
        for se in media.get("streamingEpisodes") or []:
            hit = re.search(r"Episode\s*(\d+)\s*[-–:]?\s*(.*)$", se.get("title") or "", re.I)
            if hit:
                n = int(hit.group(1))
                by_num[n] = {
                    "season": season_num,
                    "number": n,
                    "title": hit.group(2).strip() or f"Folge {n}",
                    "image": se.get("thumbnail") or "",
                    "runtime": media.get("duration"),
                    "airdate": "",
                }
        total = max(media.get("episodes") or 0, max(by_num.keys(), default=0))
        return [
            by_num.get(n) or {"season": season_num, "number": n, "title": f"Folge {n}", "image": "", "runtime": media.get("duration"), "airdate": ""}
            for n in range(1, total + 1)
        ]

    chain: list[dict] = []
    if meta["type"] != "movie":
        try:
            chain = _al_seasons(al_id)
        except Exception:  # noqa: BLE001
            chain = []
    if len(chain) > 1:
        eps = [ep for idx, node in enumerate(chain, start=1) for ep in season_eps(node, idx)]
        meta["seasons"] = [{"n": idx, "eps": node.get("episodes") or 0} for idx, node in enumerate(chain, start=1)]
        meta["totalEpisodes"] = sum(s["eps"] for s in meta["seasons"]) or meta["totalEpisodes"]
    else:
        eps = season_eps(m, 1)

    watch_list = [
        {"name": l.get("site") or "", "type": "Streamen", "logo": l.get("icon") or "", "url": l.get("url") or ""}
        for l in (m.get("externalLinks") or [])
        if l.get("type") == "STREAMING" and l.get("site")
    ]
    cast = []
    for edge in (m.get("characters") or {}).get("edges") or []:
        node = edge.get("node") or {}
        cname = (node.get("name") or {}).get("full") or ""
        if not cname:
            continue
        vas = edge.get("voiceActors") or []
        va_name = ((vas[0] or {}).get("name") or {}).get("full") if vas else ""
        cast.append({
            "name": cname,
            "role": f"Stimme: {va_name}" if va_name else ("Hauptrolle" if edge.get("role") == "MAIN" else "Nebenrolle"),
            "image": (node.get("image") or {}).get("medium") or "",
        })
    ratings = []
    if m.get("averageScore"):
        ratings.append({"label": "AniList", "value": f"{m['averageScore']} %", "url": meta["url"]})
    if m.get("idMal"):
        ratings.append({"label": "MAL", "value": "↗", "url": f"https://myanimelist.net/anime/{m['idMal']}"})
    tr = m.get("trailer") or {}
    trailer = f"https://www.youtube.com/watch?v={tr['id']}" if tr.get("site") == "youtube" and tr.get("id") else ""
    return {**meta, "watch": {"link": "", "list": watch_list}, "ratings": ratings, "trailer": trailer, "cast": cast, "episodes": eps}


# ---------- Einheitliche Suche & Details ----------

def search(query: str, sources: list[str] | None = None, tmdb_key: str = "") -> dict:
    q = (query or "").strip()
    if not q:
        return {"results": [], "errors": []}
    enabled = list(sources or ["tmdb", "tvmaze", "anilist"])
    tasks: list[tuple[str, callable]] = []
    if "tmdb" in enabled and (tmdb_key or "").strip():
        tasks.append(("TMDB Serien", lambda: [_from_tmdb_tv(t) for t in (_tmdb_fetch("/search/tv", tmdb_key, {"query": q}).get("results") or [])[:15]]))
        tasks.append(("TMDB Filme", lambda: [_from_tmdb_movie(t) for t in (_tmdb_fetch("/search/movie", tmdb_key, {"query": q}).get("results") or [])[:15]]))
    if "tvmaze" in enabled:
        tasks.append(("TVmaze", lambda: [_from_tvmaze(x["show"]) for x in (_get_json("https://api.tvmaze.com/search/shows?q=" + urllib.parse.quote(q)) or []) if isinstance(x, dict) and x.get("show")]))
    if "anilist" in enabled:
        al_q = f"query ($q: String) {{ Page(perPage: 12) {{ media(search: $q, type: ANIME, sort: SEARCH_MATCH) {{ {_AL_FIELDS} }} }} }}"
        tasks.append(("AniList", lambda: [_from_anilist(m) for m in ((_anilist_query(al_q, {"q": q}).get("Page") or {}).get("media") or []) if m.get("format") != "MUSIC"]))

    out: list[dict] = []
    errors: list[str] = []
    if not tasks:
        return {"results": [], "errors": ["Keine Datenquelle aktiv"]}

    with ThreadPoolExecutor(max_workers=len(tasks)) as pool_exec:
        futs = [(label, pool_exec.submit(fn)) for label, fn in tasks]
        for label, fut in futs:
            try:
                out.extend(fut.result())
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{label}: {exc}")

    nq = norm(q)

    def fit(r: dict) -> float:
        names = [norm(x) for x in [r.get("name"), *(r.get("altNames") or [])] if x]
        if nq in names:
            return 3.0
        if any(x.startswith(nq) for x in names):
            return 2.5
        if any(nq in x for x in names):
            return 1.6
        return 0.6

    for r in out:
        r["_score"] = (
            fit(r) * 1.5
            + (r.get("pop") or 0) * 4.0
            + (0.3 if r.get("poster") else 0.0)
            + (0.1 if (r.get("year") or 0) >= 2000 else 0.0)
        )
    out.sort(key=lambda r: r["_score"], reverse=True)

    seen: set[str] = set()
    results: list[dict] = []
    for r in out:
        k = f"{r.get('type')}:{norm(r.get('name', ''))}:{r.get('year') or ''}"
        if k in seen:
            continue
        seen.add(k)
        r.pop("_score", None)
        results.append(r)
    return {"results": results, "errors": errors}


def details(ref: str, tmdb_key: str = "") -> dict:
    prov, _, raw_id = (ref or "").partition(":")
    if prov == "tmdb":
        return _from_tmdb_tv(_tmdb_fetch(f"/tv/{raw_id}", tmdb_key))
    if prov == "tmdbmovie":
        return _from_tmdb_movie(_tmdb_fetch(f"/movie/{raw_id}", tmdb_key))
    if prov == "tvmaze":
        s = _get_json(f"https://api.tvmaze.com/shows/{urllib.parse.quote(raw_id)}?embed[]=seasons&embed[]=episodes")
        return _from_tvmaze(s if isinstance(s, dict) else {})
    if prov == "anilist":
        q = f"query ($id: Int) {{ Media(id: $id, type: ANIME) {{ {_AL_FIELDS} }} }}"
        m = _anilist_query(q, {"id": int(raw_id)}).get("Media") or {}
        return _from_anilist(m)
    raise ValueError(f"Unbekannte Quelle: {ref}")


def full(ref: str, tmdb_key: str = "") -> dict:
    prov, _, raw_id = (ref or "").partition(":")
    if prov == "tmdb":
        return _full_tmdb_tv(raw_id, tmdb_key)
    if prov == "tmdbmovie":
        return _full_tmdb_movie(raw_id, tmdb_key)
    if prov == "tvmaze":
        return _full_tvmaze(raw_id, tmdb_key)
    if prov == "anilist":
        return _full_anilist(raw_id)
    raise ValueError(f"Unbekannte Quelle: {ref}")


def season_episodes(ref: str, season: int, tmdb_key: str = "") -> list[dict]:
    prov, _, raw_id = (ref or "").partition(":")
    if prov == "tmdb":
        return _tmdb_season(raw_id, season, tmdb_key)
    data = full(ref, tmdb_key)
    return [ep for ep in (data.get("episodes") or []) if ep.get("season") == season]


# ---------- Bild-Cache (Poster & Anbieter-Logos lokal ausliefern, CSP bleibt streng) ----------

def is_allowed_image_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in IMAGE_HOSTS


def _cache_dir() -> Path:
    d = data_dir() / "mediathek_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch_cached_image(url: str) -> tuple[bytes, str] | None:
    if not is_allowed_image_url(url):
        return None
    cdir = _cache_dir()
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    img_path = cdir / key
    meta_path = cdir / f"{key}.ctype"
    if img_path.is_file():
        try:
            ctype = meta_path.read_text().strip() if meta_path.is_file() else "image/jpeg"
            os.utime(img_path, None)
            return img_path.read_bytes(), ctype
        except OSError:
            pass
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": "cloudy-speicher/1.0"})
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
            if not ctype.startswith("image/"):
                return None
            data = resp.read(5 * 1024 * 1024 + 1)
            if len(data) > 5 * 1024 * 1024:
                return None
    except Exception:  # noqa: BLE001
        return None

    try:
        _trim_image_cache(cdir)
        tmp = cdir / f".{key}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, img_path)
        meta_path.write_text(ctype)
    except OSError:
        pass
    return data, ctype


def _trim_image_cache(cdir: Path) -> None:
    try:
        files = [p for p in cdir.iterdir() if p.is_file() and not p.name.endswith(".ctype") and not p.name.startswith(".")]
        total = sum(p.stat().st_size for p in files)
        if total <= _CACHE_DIR_MAX_BYTES:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for p in files:
            if total <= _CACHE_DIR_MAX_BYTES * 0.7:
                break
            sz = p.stat().st_size
            p.unlink(missing_ok=True)
            (cdir / f"{p.name}.ctype").unlink(missing_ok=True)
            total -= sz
    except OSError:
        pass
