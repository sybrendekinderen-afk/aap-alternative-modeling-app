from flask import Flask, render_template, request, redirect, g
import base64
import importlib
import json
import os
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests as http_requests

app = Flask(__name__)


@app.after_request
def add_save_conflict_hint(response):
    if response.status_code in (301, 302, 303, 307, 308):
        location = response.headers.get("Location", "")
        if location == "/":
            if getattr(g, "save_blocked", False):
                response.headers["Location"] = "/?save_blocked=1"
            elif getattr(g, "save_conflict", False):
                response.headers["Location"] = "/?save_conflict=1"
    return response

DATA_FILE = os.path.join(os.path.dirname(__file__), "data.json")
DEFAULT_EVIDENCE_RIGOR_VALUES = [
    "quantitative ablation",
    "case based isolation",
    "bundled comparison",
    "momolitic application",
]

_BLOB_STORE = "app-data"
_BLOB_KEY = "store"
_PG_TABLE = "app_data_store"
_PG_KEY = "store"


def _is_postgres_dsn(url):
    if not url:
        return False
    scheme = (urlparse(url).scheme or "").lower()
    return scheme.startswith("postgres")


def _normalize_postgres_dsn(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in ["", "localhost", "127.0.0.1"]:
        return url

    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query = {key: value for key, value in query_pairs}
    if "sslmode" not in query:
        query_pairs.append(("sslmode", "require"))
        return urlunparse(parsed._replace(query=urlencode(query_pairs)))
    return url


def _get_database_url_details():
    # Prefer explicit app-level URL, then provider-specific defaults.
    for env_name in [
        "DATABASE_URL",
        "POSTGRES_URL",
        "POSTGRES_URL_NON_POOLING",
        "POSTGRES_PRISMA_URL",
        "SUPABASE_DB_URL",
        "SUPABASE_DATABASE_URL",
        "SUPABASE_URL",
    ]:
        url = os.environ.get(env_name, "").strip()
        if not url:
            continue
        if _is_postgres_dsn(url):
            return env_name, _normalize_postgres_dsn(url)
        # Helpful in Vercel logs when SUPABASE_URL is set to an HTTPS project URL.
        print(f"Ignoring non-Postgres URL from {env_name}: scheme={urlparse(url).scheme}")
    return None, None


def _get_database_url():
    _, url = _get_database_url_details()
    return url


def _is_vercel_runtime():
    return bool(os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"))


def _get_psycopg_module():
    try:
        return importlib.import_module("psycopg")
    except Exception:
        return None


def _load_data_from_postgres():
    db_url = _get_database_url()
    psycopg = _get_psycopg_module()
    if not db_url or psycopg is None:
        if _is_vercel_runtime():
            print(
                "Postgres load skipped: "
                f"has_db_url={bool(db_url)} has_psycopg={psycopg is not None}"
            )
        return None

    try:
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {_PG_TABLE} (
                        store_key TEXT PRIMARY KEY,
                        data JSONB NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    f"SELECT data FROM {_PG_TABLE} WHERE store_key = %s",
                    (_PG_KEY,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                payload = row[0]
                if isinstance(payload, str):
                    return json.loads(payload)
                return payload
    except Exception as exc:
        print(f"Postgres load failed: {type(exc).__name__}: {exc}")
        return None


def _save_data_to_postgres(data):
    db_url = _get_database_url()
    psycopg = _get_psycopg_module()
    if not db_url or psycopg is None:
        if _is_vercel_runtime():
            print(
                "Postgres save skipped: "
                f"has_db_url={bool(db_url)} has_psycopg={psycopg is not None}"
            )
        return False

    try:
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {_PG_TABLE} (
                        store_key TEXT PRIMARY KEY,
                        data JSONB NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    f"""
                    INSERT INTO {_PG_TABLE} (store_key, data)
                    VALUES (%s, %s::jsonb)
                    ON CONFLICT (store_key)
                    DO UPDATE SET data = EXCLUDED.data, updated_at = NOW()
                    """,
                    (_PG_KEY, json.dumps(data)),
                )
            conn.commit()
        return True
    except Exception as exc:
        print(f"Postgres save failed: {type(exc).__name__}: {exc}")
        return False


def _get_blob_context():
    """Parse the Netlify Blobs context injected at runtime."""
    ctx_raw = os.environ.get("NETLIFY_BLOBS_CONTEXT", "")
    if not ctx_raw:
        return None
    try:
        padding = (4 - len(ctx_raw) % 4) % 4
        return json.loads(base64.b64decode(ctx_raw + "=" * padding))
    except Exception:
        return None


def _blob_url(ctx):
    return f"{ctx['edgeURL']}/{ctx['siteID']}/{_BLOB_STORE}/{_BLOB_KEY}"


def _load_raw_data():
    """Return parsed JSON from Postgres, Netlify Blobs, or the local data file."""

    pg_data = _load_data_from_postgres()
    if pg_data is not None:
        return pg_data

    ctx = _get_blob_context()
    if ctx:
        try:
            resp = http_requests.get(
                _blob_url(ctx),
                headers={"Authorization": f"Bearer {ctx['token']}"},
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None

    if not os.path.exists(DATA_FILE):
        return None
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_data():
    data = _load_raw_data()
    if data is None:
        return {
            "solutions": [],
            "sources": [],
            "effects": [],
            "underlying_llms": [],
            "model_types": [],
            "prompting_techniques": [],
            "other_techniques": [],
            "modeling_purposes": [],
            "evidence_rigor_values": DEFAULT_EVIDENCE_RIGOR_VALUES,
        }

    if isinstance(data, dict):
        store = {
            "solutions": data.get("solutions", []),
            "sources": data.get("sources", []),
            "effects": data.get("effects", []),
            "underlying_llms": data.get("underlying_llms", []),
            "model_types": data.get("model_types", []),
            "prompting_techniques": data.get("prompting_techniques", []),
            "other_techniques": data.get("other_techniques", []),
            "modeling_purposes": data.get("modeling_purposes", []),
            "evidence_rigor_values": data.get("evidence_rigor_values", DEFAULT_EVIDENCE_RIGOR_VALUES)
        }

        normalized_underlying_llms = []
        for llm in store.get("underlying_llms", []):
            if not isinstance(llm, dict):
                continue
            try:
                llm_id = int(llm.get("id"))
            except (TypeError, ValueError):
                llm_id = get_next_underlying_llm_id(normalized_underlying_llms)
            llm["id"] = llm_id
            normalized_underlying_llms.append(normalize_underlying_llm_record(llm))
        store["underlying_llms"] = normalized_underlying_llms

        normalized_sources = []
        for source in store.get("sources", []):
            if not isinstance(source, dict):
                continue
            try:
                source_id = int(source.get("id"))
            except (TypeError, ValueError):
                source_id = get_next_source_id(normalized_sources)
            source["id"] = source_id
            normalized_sources.append(normalize_source_record(source))
        store["sources"] = normalized_sources

        for solution in store.get("solutions", []):
            normalized_source_ids = []
            for source_id in solution.get("source_ids", []):
                try:
                    normalized_source_ids.append(int(source_id))
                except (TypeError, ValueError):
                    continue

            for legacy_source in solution.get("sources", []):
                if not isinstance(legacy_source, dict):
                    continue
                migrated_source = normalize_source_record(dict(legacy_source))
                migrated_source["id"] = get_next_source_id(store["sources"])
                store["sources"].append(migrated_source)
                normalized_source_ids.append(migrated_source["id"])

            unique_source_ids = list(dict.fromkeys(normalized_source_ids))
            existing_source_id = solution.get("source_id")
            try:
                existing_source_id = int(existing_source_id)
            except (TypeError, ValueError):
                existing_source_id = None
            solution["source_id"] = existing_source_id if existing_source_id is not None else (unique_source_ids[0] if unique_source_ids else None)
            solution.pop("source_ids", None)
            solution.pop("sources", None)

            normalized_other_technique_ids = []
            for technique_id in solution.get("other_technique_ids", []):
                try:
                    normalized_other_technique_ids.append(int(technique_id))
                except (TypeError, ValueError):
                    continue
            solution["other_technique_ids"] = list(dict.fromkeys(normalized_other_technique_ids))

        valid_source_ids = {source.get("id") for source in store.get("sources", [])}
        fallback_source_id = next(iter(valid_source_ids), None)
        for solution in store.get("solutions", []):
            if solution.get("source_id") not in valid_source_ids:
                solution["source_id"] = fallback_source_id

        for modeling_purpose in store.get("modeling_purposes", []):
            modeling_purpose.pop("description", None)
            parent_id = modeling_purpose.get("parent_id")
            modeling_purpose["parent_id"] = int(parent_id) if parent_id not in [None, ""] else None
            model_type_id = modeling_purpose.get("model_type_id")
            try:
                modeling_purpose["model_type_id"] = int(model_type_id)
            except (TypeError, ValueError):
                modeling_purpose["model_type_id"] = None

        normalized_effects = []
        for effect in store.get("effects", []):
            if not isinstance(effect, dict):
                continue
            normalized_effects.append(normalize_effect_record(effect))

        next_effect_id = get_next_effect_id(normalized_effects)
        for source in store.get("sources", []):
            for effect in source.pop("effects", []):
                if not isinstance(effect, dict):
                    continue
                effect_payload = normalize_effect_record({
                    "id": next_effect_id,
                    "description": effect.get("description", ""),
                    "evidence_rigor": effect.get("evidence_rigor", ""),
                    "solution_id": None,
                    "prompting_technique_id": None,
                    "other_technique_id": None,
                    "underlying_llm_id": None,
                })
                normalized_effects.append(effect_payload)
                next_effect_id += 1

        store["effects"] = normalized_effects

        removed_prompting_ids = {
            tech.get("id")
            for tech in store.get("prompting_techniques", [])
            if str(tech.get("name", "")).strip().lower() == "unspecified"
        }
        removed_other_ids = {
            tech.get("id")
            for tech in store.get("other_techniques", [])
            if str(tech.get("name", "")).strip().lower() == "unspecified"
        }

        store["prompting_techniques"] = [
            tech for tech in store.get("prompting_techniques", [])
            if tech.get("id") not in removed_prompting_ids
        ]
        store["other_techniques"] = [
            tech for tech in store.get("other_techniques", [])
            if tech.get("id") not in removed_other_ids
        ]

        for solution in store.get("solutions", []):
            solution["prompting_technique_ids"] = [
                tid for tid in solution.get("prompting_technique_ids", [])
                if tid not in removed_prompting_ids
            ]
            solution["other_technique_ids"] = [
                tid for tid in solution.get("other_technique_ids", [])
                if tid not in removed_other_ids
            ]

        for effect in store.get("effects", []):
            if effect.get("prompting_technique_id") in removed_prompting_ids:
                effect["prompting_technique_id"] = None
            if effect.get("other_technique_id") in removed_other_ids:
                effect["other_technique_id"] = None

        valid_model_type_ids = {model_type.get("id") for model_type in store.get("model_types", [])}
        for modeling_purpose in store.get("modeling_purposes", []):
            if modeling_purpose.get("model_type_id") not in valid_model_type_ids:
                modeling_purpose["model_type_id"] = None

        valid_solution_ids = {solution.get("id") for solution in store.get("solutions", [])}
        valid_prompting_ids = {tech.get("id") for tech in store.get("prompting_techniques", [])}
        valid_other_ids = {tech.get("id") for tech in store.get("other_techniques", [])}
        valid_underlying_llm_ids = {llm.get("id") for llm in store.get("underlying_llms", [])}
        for effect in store.get("effects", []):
            if effect.get("solution_id") not in valid_solution_ids:
                effect["solution_id"] = None
            if effect.get("prompting_technique_id") not in valid_prompting_ids:
                effect["prompting_technique_id"] = None
            if effect.get("other_technique_id") not in valid_other_ids:
                effect["other_technique_id"] = None
            if effect.get("underlying_llm_id") not in valid_underlying_llm_ids:
                effect["underlying_llm_id"] = None

        normalize_root_purpose_order(store.get("modeling_purposes", []))

        evidence_values = list(store.get("evidence_rigor_values", []))
        for effect in store.get("effects", []):
            value = effect.get("evidence_rigor", "").strip()
            if value:
                evidence_values.append(value)
        store["evidence_rigor_values"] = list(dict.fromkeys([value for value in evidence_values if value]))

        return store

    if isinstance(data, list):
        return {
            "solutions": data,
            "sources": [],
            "effects": [],
            "underlying_llms": [],
            "model_types": [],
            "prompting_techniques": [],
            "other_techniques": [],
            "modeling_purposes": [],
            "evidence_rigor_values": DEFAULT_EVIDENCE_RIGOR_VALUES,
        }

    return {
        "solutions": [],
        "sources": [],
        "effects": [],
        "underlying_llms": [],
        "model_types": [],
        "prompting_techniques": [],
        "other_techniques": [],
        "modeling_purposes": [],
        "evidence_rigor_values": DEFAULT_EVIDENCE_RIGOR_VALUES,
    }


def save_data(data):
    if _save_data_to_postgres(data):
        return

    ctx = _get_blob_context()
    if ctx:
        try:
            http_requests.put(
                _blob_url(ctx),
                data=json.dumps(data),
                headers={
                    "Authorization": f"Bearer {ctx['token']}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            return
        except Exception:
            pass
    # On Vercel, filesystem writes are ephemeral and can fail. Avoid surfacing a 500
    # if remote persistence had a transient failure.
    if _is_vercel_runtime():
        g.save_blocked = True
        return

    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def find_solution(solutions, solution_id):
    for solution in solutions:
        if solution["id"] == solution_id:
            return solution
    return None


def get_model_type_lookup(model_types):
    return {model_type["id"]: model_type for model_type in model_types}


def get_prompting_technique_lookup(prompting_techniques):
    return {technique["id"]: technique for technique in prompting_techniques}


def get_other_technique_lookup(other_techniques):
    return {technique["id"]: technique for technique in other_techniques}


def get_underlying_llm_lookup(underlying_llms):
    return {llm["id"]: llm for llm in underlying_llms}


def get_modeling_purpose_lookup(modeling_purposes):
    return {purpose["id"]: purpose for purpose in modeling_purposes}


def get_modeling_purpose_solution_lookup(solutions):
    lookup = {}
    for solution in solutions:
        for purpose_id in solution.get("modeling_purpose_ids", []):
            purpose_key = int(purpose_id)
            lookup.setdefault(purpose_key, []).append(solution)
    return lookup


def get_next_source_id(sources):
    return max([source.get("id", 0) for source in sources], default=0) + 1


def get_next_effect_id(effects):
    return max([effect.get("id", 0) for effect in effects], default=0) + 1


def get_next_underlying_llm_id(underlying_llms):
    return max([llm.get("id", 0) for llm in underlying_llms], default=0) + 1


def normalize_source_record(source):
    source.pop("effects", None)
    source.pop("effect", None)
    source.pop("effect_ids", None)
    source.pop("link", None)
    source.pop("model_type_id", None)
    source["title"] = str(source.get("title", "") or "")
    source["author"] = str(source.get("author", "") or "")
    source["year"] = normalize_source_year(source.get("year", ""))
    source["doi"] = str(source.get("doi", "") or "")
    return source


def normalize_effect_record(effect):
    try:
        effect_id = int(effect.get("id"))
    except (TypeError, ValueError):
        effect_id = 0

    solution_id = effect.get("solution_id")
    try:
        solution_id = int(solution_id)
    except (TypeError, ValueError):
        solution_id = None

    prompting_technique_id = effect.get("prompting_technique_id")
    try:
        prompting_technique_id = int(prompting_technique_id)
    except (TypeError, ValueError):
        prompting_technique_id = None

    other_technique_id = effect.get("other_technique_id")
    try:
        other_technique_id = int(other_technique_id)
    except (TypeError, ValueError):
        other_technique_id = None

    underlying_llm_id = effect.get("underlying_llm_id")
    try:
        underlying_llm_id = int(underlying_llm_id)
    except (TypeError, ValueError):
        underlying_llm_id = None

    return {
        "id": effect_id,
        "description": str(effect.get("description", "") or "").strip(),
        "evidence_rigor": str(effect.get("evidence_rigor", "") or "").strip(),
        "solution_id": solution_id,
        "prompting_technique_id": prompting_technique_id,
        "other_technique_id": other_technique_id,
        "underlying_llm_id": underlying_llm_id,
    }


def normalize_underlying_llm_record(llm):
    return {
        "id": int(llm.get("id", 0)),
        "name": str(llm.get("name", "") or "").strip(),
        "version": str(llm.get("version", "") or "").strip(),
    }


def ensure_default_techniques(store):
    return store


def get_solution_source_lookup(solutions, sources):
    source_lookup = {source["id"]: source for source in sources}
    lookup = {}
    for solution in solutions:
        resolved_source = None
        source_id = solution.get("source_id")
        if source_id is not None:
            resolved_source = source_lookup.get(source_id)
        lookup[solution.get("id")] = resolved_source
    return lookup


def get_source_solution_lookup(solutions, sources):
    solution_lookup = {solution["id"]: solution for solution in solutions}
    lookup = {}
    for source in sources:
        linked_solutions = []
        source_id = source.get("id")
        for solution in solutions:
            if source_id == solution.get("source_id"):
                resolved = solution_lookup.get(solution.get("id"))
                if resolved:
                    linked_solutions.append(resolved)
        lookup[source_id] = linked_solutions
    return lookup


def get_solution_model_type_lookup(solutions, modeling_purposes):
    purpose_lookup = {purpose["id"]: purpose for purpose in modeling_purposes}

    def collect_model_type_ids_with_root_ancestors(purpose_id):
        model_type_ids = []
        visited = set()
        current = purpose_lookup.get(purpose_id)

        # Include current purpose and walk to root to inherit root-associated model types.
        while current and current.get("id") not in visited:
            visited.add(current.get("id"))
            model_type_id = current.get("model_type_id")
            if model_type_id not in [None, ""]:
                model_type_ids.append(str(model_type_id))

            parent_id = current.get("parent_id")
            current = purpose_lookup.get(parent_id) if parent_id is not None else None

        return model_type_ids

    lookup = {}
    for solution in solutions:
        model_type_ids = []
        for purpose_id in solution.get("modeling_purpose_ids", []):
            model_type_ids.extend(collect_model_type_ids_with_root_ancestors(purpose_id))
        lookup[solution.get("id")] = list(dict.fromkeys(model_type_ids))
    return lookup


def get_solution_effect_lookup(effects):
    lookup = {}
    for effect in effects:
        solution_id = effect.get("solution_id")
        if solution_id is None:
            continue
        lookup.setdefault(solution_id, []).append(effect)
    return lookup


def get_solution_underlying_llm_lookup(solutions, effects):
    lookup = {solution.get("id"): [] for solution in solutions}
    for effect in effects:
        solution_id = effect.get("solution_id")
        underlying_llm_id = effect.get("underlying_llm_id")
        if solution_id is None or underlying_llm_id in [None, ""]:
            continue
        lookup.setdefault(solution_id, []).append(str(underlying_llm_id))

    for solution_id, llm_ids in lookup.items():
        lookup[solution_id] = list(dict.fromkeys(llm_ids))
    return lookup


def normalize_root_purpose_order(modeling_purposes):
    indexed = list(enumerate(modeling_purposes))
    root_items = []
    for idx, purpose in indexed:
        if purpose.get("parent_id") is None:
            raw_order = purpose.get("root_order")
            try:
                parsed_order = int(raw_order)
                has_order = 0
            except (TypeError, ValueError):
                parsed_order = idx
                has_order = 1
            root_items.append((has_order, parsed_order, idx, purpose))
        else:
            purpose.pop("root_order", None)

    root_items.sort(key=lambda item: (item[0], item[1], item[2]))
    for position, (_, _, _, purpose) in enumerate(root_items):
        purpose["root_order"] = position


def build_modeling_purpose_tree(modeling_purposes):
    purpose_lookup = {purpose["id"]: {**purpose, "children": []} for purpose in modeling_purposes}
    roots = []

    for purpose in purpose_lookup.values():
        parent_id = purpose.get("parent_id")
        if parent_id in purpose_lookup:
            purpose_lookup[parent_id]["children"].append(purpose)
        else:
            roots.append(purpose)

    roots.sort(key=lambda node: (node.get("root_order", 10**9), node.get("name", "").lower()))

    def sort_children(nodes):
        nodes.sort(key=lambda node: node.get("name", "").lower())
        for node in nodes:
            sort_children(node["children"])

    for root in roots:
        sort_children(root["children"])
    return roots


def is_valid_modeling_purpose_parent(purpose_id, parent_id, modeling_purposes):
    if not parent_id:
        return True

    if purpose_id is not None and int(parent_id) == int(purpose_id):
        return False

    purpose_lookup = {purpose["id"]: purpose for purpose in modeling_purposes}
    current_parent_id = int(parent_id)
    while current_parent_id:
        if purpose_id is not None and current_parent_id == int(purpose_id):
            return False
        parent_purpose = purpose_lookup.get(current_parent_id)
        if not parent_purpose:
            break
        current_parent_id = parent_purpose.get("parent_id")
    return True


def resolve_model_type_value(model_type_id, model_types):
    if not model_type_id:
        return None

    if str(model_type_id).strip().lower() == "agnostic":
        return "agnostic"

    try:
        model_type_id = int(model_type_id)
    except (TypeError, ValueError):
        return None

    selected_model_type = next((mt for mt in model_types if mt.get("id") == model_type_id), None)
    return selected_model_type["id"] if selected_model_type else None


def normalize_source_year(raw_year):
    year = str(raw_year or "").strip()
    if not year:
        return ""
    if year.isdigit() and len(year) == 4:
        return year
    return ""


def parse_optional_int(raw_value):
    if raw_value is None:
        return None
    text = str(raw_value).strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def parse_int_list(raw_values):
    values = []
    for raw in raw_values:
        parsed = parse_optional_int(raw)
        if parsed is not None:
            values.append(parsed)
    return values


def parse_effects_payload(raw_payload):
    if not raw_payload:
        return []
    try:
        payload = json.loads(raw_payload)
    except Exception:
        return []

    parsed_effects = []
    if not isinstance(payload, list):
        return parsed_effects

    for item in payload:
        if not isinstance(item, dict):
            continue
        normalized = normalize_effect_record(item)
        if normalized.get("description"):
            parsed_effects.append(normalized)
    return parsed_effects


def update_evidence_rigor_values(store, effects):
    values = list(store.get("evidence_rigor_values", DEFAULT_EVIDENCE_RIGOR_VALUES))
    for effect in effects:
        value = effect.get("evidence_rigor", "").strip()
        if value:
            values.append(value)
    store["evidence_rigor_values"] = list(dict.fromkeys([value for value in values if value]))


@app.route("/")
def home():
    store = load_data()
    solutions = store.get("solutions", [])
    sources = store.get("sources", [])
    effects = store.get("effects", [])
    underlying_llms = store.get("underlying_llms", [])
    model_types = store.get("model_types", [])
    prompting_techniques = store.get("prompting_techniques", [])
    other_techniques = store.get("other_techniques", [])
    modeling_purposes = store.get("modeling_purposes", [])
    return render_template(
        "index.html",
        solutions=solutions,
        sources=sources,
        effects=effects,
        underlying_llms=underlying_llms,
        solution_source_lookup=get_solution_source_lookup(solutions, sources),
        solution_effect_lookup=get_solution_effect_lookup(effects),
        solution_underlying_llm_lookup=get_solution_underlying_llm_lookup(solutions, effects),
        source_solution_lookup=get_source_solution_lookup(solutions, sources),
        solution_model_type_lookup=get_solution_model_type_lookup(solutions, modeling_purposes),
        model_types=model_types,
        model_type_lookup=get_model_type_lookup(model_types),
        prompting_techniques=prompting_techniques,
        prompting_technique_lookup=get_prompting_technique_lookup(prompting_techniques),
        other_techniques=other_techniques,
        other_technique_lookup=get_other_technique_lookup(other_techniques),
        underlying_llm_lookup=get_underlying_llm_lookup(underlying_llms),
        modeling_purposes=modeling_purposes,
        modeling_purpose_lookup=get_modeling_purpose_lookup(modeling_purposes),
        modeling_purpose_tree=build_modeling_purpose_tree(modeling_purposes),
        modeling_purpose_solution_lookup=get_modeling_purpose_solution_lookup(solutions),
        evidence_rigor_values=store.get("evidence_rigor_values", DEFAULT_EVIDENCE_RIGOR_VALUES)
    )


@app.route("/add_solution", methods=["POST"])
def add_solution():
    store = load_data()
    solutions = store.get("solutions", [])
    sources = store.get("sources", [])

    next_id = max([s["id"] for s in solutions], default=0) + 1

    source_id = parse_optional_int(request.form.get("source_id", ""))
    if source_id is None:
        return redirect("/")

    if not any(source.get("id") == source_id for source in sources):
        return redirect("/")

    prompting_technique_ids = parse_int_list(request.form.getlist("prompting_technique_ids"))
    other_technique_ids = parse_int_list(request.form.getlist("other_technique_ids"))
    modeling_purpose_ids = parse_int_list(request.form.getlist("modeling_purpose_ids"))

    solutions.append({
        "id": next_id,
        "name": request.form["name"],
        "prompting_technique_ids": prompting_technique_ids,
        "other_technique_ids": other_technique_ids,
        "modeling_purpose_ids": modeling_purpose_ids,
        "justification": request.form.get("justification", ""),
        "source_id": source_id
    })

    store["solutions"] = solutions
    save_data(store)
    return redirect("/")


@app.route("/add_modeling_purpose", methods=["POST"])
def add_modeling_purpose():
    store = load_data()
    name = request.form.get("modeling_purpose_name", "").strip()
    parent_id = parse_optional_int(request.form.get("parent_id", ""))
    model_type_id = parse_optional_int(request.form.get("model_type_id", ""))

    if name:
        modeling_purposes = store.get("modeling_purposes", [])
        parent_value = parent_id
        model_type_value = model_type_id
        if is_valid_modeling_purpose_parent(None, parent_value, modeling_purposes):
            new_root_order = len([mp for mp in modeling_purposes if mp.get("parent_id") is None]) if parent_value is None else None
            modeling_purposes.append({
                "id": max([mp["id"] for mp in modeling_purposes], default=0) + 1,
                "name": name,
                "parent_id": parent_value,
                "model_type_id": model_type_value,
                "root_order": new_root_order
            })
            normalize_root_purpose_order(modeling_purposes)
            store["modeling_purposes"] = modeling_purposes
            save_data(store)

    return redirect("/")


@app.route("/update_modeling_purpose/<int:modeling_purpose_id>", methods=["POST"])
def update_modeling_purpose(modeling_purpose_id):
    store = load_data()
    modeling_purposes = store.get("modeling_purposes", [])
    parent_value = parse_optional_int(request.form.get("parent_id", ""))
    model_type_value = parse_optional_int(request.form.get("model_type_id", ""))

    for modeling_purpose in modeling_purposes:
        if modeling_purpose["id"] == modeling_purpose_id:
            previous_parent_id = modeling_purpose.get("parent_id")
            modeling_purpose["name"] = request.form.get("modeling_purpose_name", modeling_purpose["name"]).strip()
            modeling_purpose["model_type_id"] = model_type_value
            if is_valid_modeling_purpose_parent(modeling_purpose_id, parent_value, modeling_purposes):
                modeling_purpose["parent_id"] = parent_value
                if previous_parent_id is None and parent_value is not None:
                    modeling_purpose.pop("root_order", None)
                elif previous_parent_id is not None and parent_value is None:
                    modeling_purpose["root_order"] = len([mp for mp in modeling_purposes if mp.get("parent_id") is None and mp.get("id") != modeling_purpose_id])
            break

    normalize_root_purpose_order(modeling_purposes)
    store["modeling_purposes"] = modeling_purposes
    save_data(store)
    return redirect("/")


@app.route("/move_modeling_purpose_root/<int:modeling_purpose_id>/<string:direction>")
def move_modeling_purpose_root(modeling_purpose_id, direction):
    if direction not in ["up", "down"]:
        return redirect("/")

    store = load_data()
    modeling_purposes = store.get("modeling_purposes", [])
    normalize_root_purpose_order(modeling_purposes)
    roots = [mp for mp in modeling_purposes if mp.get("parent_id") is None]
    roots.sort(key=lambda mp: mp.get("root_order", 10**9))

    index = next((i for i, mp in enumerate(roots) if mp.get("id") == modeling_purpose_id), None)
    if index is None:
        return redirect("/")

    if direction == "up" and index > 0:
        roots[index - 1], roots[index] = roots[index], roots[index - 1]
    elif direction == "down" and index < len(roots) - 1:
        roots[index + 1], roots[index] = roots[index], roots[index + 1]

    for position, purpose in enumerate(roots):
        purpose["root_order"] = position

    store["modeling_purposes"] = modeling_purposes
    save_data(store)
    return redirect("/")


@app.route("/delete_modeling_purpose/<int:modeling_purpose_id>")
def delete_modeling_purpose(modeling_purpose_id):
    store = load_data()
    modeling_purposes = store.get("modeling_purposes", [])
    store["modeling_purposes"] = []
    for modeling_purpose in modeling_purposes:
        if modeling_purpose["id"] == modeling_purpose_id:
            continue
        if modeling_purpose.get("parent_id") == modeling_purpose_id:
            modeling_purpose["parent_id"] = None
        store["modeling_purposes"].append(modeling_purpose)

    normalize_root_purpose_order(store["modeling_purposes"])

    for solution in store.get("solutions", []):
        solution["modeling_purpose_ids"] = [
            pid for pid in solution.get("modeling_purpose_ids", [])
            if pid != modeling_purpose_id
        ]

    save_data(store)
    return redirect("/")


@app.route("/add_model_type", methods=["POST"])
def add_model_type():
    store = load_data()
    name = request.form.get("model_type_name", "").strip()
    notation = request.form.get("model_type_notation", "").strip()

    if name or notation:
        model_types = store.get("model_types", [])
        existing = next((mt for mt in model_types if mt.get("name") == name and mt.get("notation") == notation), None)
        if not existing:
            model_types.append({
                "id": max([mt["id"] for mt in model_types], default=0) + 1,
                "name": name,
                "notation": notation
            })
            store["model_types"] = model_types
            save_data(store)

    return redirect("/")


@app.route("/update_model_type/<int:model_type_id>", methods=["POST"])
def update_model_type(model_type_id):
    store = load_data()
    model_types = store.get("model_types", [])
    for model_type in model_types:
        if model_type["id"] == model_type_id:
            model_type["name"] = request.form.get("model_type_name", model_type["name"]).strip()
            model_type["notation"] = request.form.get("model_type_notation", model_type["notation"]).strip()
            break
    store["model_types"] = model_types
    save_data(store)
    return redirect("/")


@app.route("/delete_model_type/<int:model_type_id>")
def delete_model_type(model_type_id):
    store = load_data()
    model_types = store.get("model_types", [])
    store["model_types"] = [mt for mt in model_types if mt["id"] != model_type_id]
    for modeling_purpose in store.get("modeling_purposes", []):
        if modeling_purpose.get("model_type_id") == model_type_id:
            modeling_purpose["model_type_id"] = None

    save_data(store)
    return redirect("/")


@app.route("/add_prompting_technique", methods=["POST"])
def add_prompting_technique():
    store = load_data()
    name = request.form.get("prompting_technique_name", "").strip()
    if name:
        techniques = store.get("prompting_techniques", [])
        existing = next((pt for pt in techniques if pt.get("name") == name), None)
        if not existing:
            techniques.append({
                "id": max([pt["id"] for pt in techniques], default=0) + 1,
                "name": name
            })
            store["prompting_techniques"] = techniques
            save_data(store)
    return redirect("/")


@app.route("/update_prompting_technique/<int:prompting_technique_id>", methods=["POST"])
def update_prompting_technique(prompting_technique_id):
    store = load_data()
    techniques = store.get("prompting_techniques", [])
    for technique in techniques:
        if technique["id"] == prompting_technique_id:
            technique["name"] = request.form.get("prompting_technique_name", technique["name"]).strip()
            break
    store["prompting_techniques"] = techniques
    save_data(store)
    return redirect("/")


@app.route("/delete_prompting_technique/<int:prompting_technique_id>")
def delete_prompting_technique(prompting_technique_id):
    store = load_data()
    techniques = store.get("prompting_techniques", [])
    store["prompting_techniques"] = [pt for pt in techniques if pt["id"] != prompting_technique_id]

    for solution in store.get("solutions", []):
        solution["prompting_technique_ids"] = [
            pid for pid in solution.get("prompting_technique_ids", [])
            if pid != prompting_technique_id
        ]

    for effect in store.get("effects", []):
        if effect.get("prompting_technique_id") == prompting_technique_id:
            effect["prompting_technique_id"] = None

    save_data(store)
    return redirect("/")


@app.route("/add_other_technique", methods=["POST"])
def add_other_technique():
    store = load_data()
    name = request.form.get("other_technique_name", "").strip()
    if name:
        techniques = store.get("other_techniques", [])
        existing = next((tech for tech in techniques if tech.get("name") == name), None)
        if not existing:
            techniques.append({
                "id": max([tech["id"] for tech in techniques], default=0) + 1,
                "name": name
            })
            store["other_techniques"] = techniques
            save_data(store)
    return redirect("/")


@app.route("/update_other_technique/<int:other_technique_id>", methods=["POST"])
def update_other_technique(other_technique_id):
    store = load_data()
    techniques = store.get("other_techniques", [])
    for technique in techniques:
        if technique["id"] == other_technique_id:
            technique["name"] = request.form.get("other_technique_name", technique["name"]).strip()
            break
    store["other_techniques"] = techniques
    save_data(store)
    return redirect("/")


@app.route("/delete_other_technique/<int:other_technique_id>")
def delete_other_technique(other_technique_id):
    store = load_data()
    techniques = store.get("other_techniques", [])
    store["other_techniques"] = [tech for tech in techniques if tech["id"] != other_technique_id]

    for solution in store.get("solutions", []):
        solution["other_technique_ids"] = [
            tid for tid in solution.get("other_technique_ids", [])
            if tid != other_technique_id
        ]

    for effect in store.get("effects", []):
        if effect.get("other_technique_id") == other_technique_id:
            effect["other_technique_id"] = None

    save_data(store)
    return redirect("/")


@app.route("/add_underlying_llm", methods=["POST"])
def add_underlying_llm():
    store = load_data()
    underlying_llms = store.get("underlying_llms", [])
    name = request.form.get("underlying_llm_name", "").strip()
    version = request.form.get("underlying_llm_version", "").strip()
    if name:
        existing = next(
            (
                llm for llm in underlying_llms
                if llm.get("name", "").strip().lower() == name.lower()
                and llm.get("version", "").strip() == version
            ),
            None,
        )
        if not existing:
            underlying_llms.append({
                "id": get_next_underlying_llm_id(underlying_llms),
                "name": name,
                "version": version,
            })
            store["underlying_llms"] = underlying_llms
            save_data(store)
    return redirect("/")


@app.route("/update_underlying_llm/<int:underlying_llm_id>", methods=["POST"])
def update_underlying_llm(underlying_llm_id):
    store = load_data()
    underlying_llms = store.get("underlying_llms", [])
    for llm in underlying_llms:
        if llm.get("id") == underlying_llm_id:
            llm["name"] = request.form.get("underlying_llm_name", llm.get("name", "")).strip()
            llm["version"] = request.form.get("underlying_llm_version", llm.get("version", "")).strip()
            break
    store["underlying_llms"] = underlying_llms
    save_data(store)
    return redirect("/")


@app.route("/delete_underlying_llm/<int:underlying_llm_id>")
def delete_underlying_llm(underlying_llm_id):
    store = load_data()
    store["underlying_llms"] = [
        llm for llm in store.get("underlying_llms", [])
        if llm.get("id") != underlying_llm_id
    ]
    for effect in store.get("effects", []):
        if effect.get("underlying_llm_id") == underlying_llm_id:
            effect["underlying_llm_id"] = None
    save_data(store)
    return redirect("/")


@app.route("/edit_solution/<int:solution_id>")
def edit_solution(solution_id):
    store = load_data()
    solution = find_solution(store.get("solutions", []), solution_id)
    if not solution:
        return redirect("/")
    return render_template(
        "edit_paper.html",
        solution=solution,
        mode="solution",
        sources=store.get("sources", []),
        prompting_techniques=store.get("prompting_techniques", []),
        other_techniques=store.get("other_techniques", []),
        modeling_purposes=store.get("modeling_purposes", [])
    )


@app.route("/update_solution/<int:solution_id>", methods=["POST"])
def update_solution(solution_id):
    store = load_data()
    solution = find_solution(store.get("solutions", []), solution_id)
    if solution:
        solution["name"] = request.form.get("name", solution["name"])
        source_id = parse_optional_int(request.form.get("source_id", ""))
        source_exists = any(source.get("id") == source_id for source in store.get("sources", []))
        prompting_technique_ids = parse_int_list(request.form.getlist("prompting_technique_ids"))
        other_technique_ids = parse_int_list(request.form.getlist("other_technique_ids"))
        modeling_purpose_ids = parse_int_list(request.form.getlist("modeling_purpose_ids"))
        if source_exists:
            solution["source_id"] = source_id
        solution["prompting_technique_ids"] = prompting_technique_ids
        solution["other_technique_ids"] = other_technique_ids
        solution["modeling_purpose_ids"] = modeling_purpose_ids
        solution["justification"] = request.form.get("justification", solution["justification"])
        save_data(store)
    return redirect("/")


@app.route("/add_source", methods=["POST"])
def add_source():
    store = load_data()
    sources = store.get("sources", [])

    new_source = {
        "id": get_next_source_id(sources),
        "title": request.form.get("title", ""),
        "author": request.form.get("author", ""),
        "year": normalize_source_year(request.form.get("year", "")),
        "doi": request.form.get("doi", "")
    }
    sources.append(new_source)

    store["sources"] = sources
    save_data(store)
    return redirect("/")


@app.route("/edit_source/<int:source_id>")
@app.route("/edit_source/<int:solution_id>/<int:source_id>")
def edit_source(source_id, solution_id=None):
    store = load_data()
    source = next((s for s in store.get("sources", []) if s["id"] == source_id), None)
    if not source:
        return redirect("/")

    return render_template(
        "edit_paper.html",
        source=source,
        mode="source",
    )


@app.route("/update_source/<int:source_id>", methods=["POST"])
@app.route("/update_source/<int:solution_id>/<int:source_id>", methods=["POST"])
def update_source(source_id, solution_id=None):
    store = load_data()
    sources = store.get("sources", [])

    source = next((entry for entry in sources if entry["id"] == source_id), None)
    if source:
        source["title"] = request.form.get("title", source["title"])
        source["author"] = request.form.get("author", source["author"])
        source["year"] = normalize_source_year(request.form.get("year", source.get("year", "")))
        source["doi"] = request.form.get("doi", source["doi"])
        source.pop("link", None)
        store["sources"] = sources
        save_data(store)
    return redirect("/")


@app.route("/add_effect", methods=["POST"])
def add_effect():
    store = load_data()
    effects = store.get("effects", [])

    solution_id = parse_optional_int(request.form.get("solution_id", ""))
    if solution_id is not None and not any(solution.get("id") == solution_id for solution in store.get("solutions", [])):
        solution_id = None

    prompting_technique_id = parse_optional_int(request.form.get("prompting_technique_id", ""))
    other_technique_id = parse_optional_int(request.form.get("other_technique_id", ""))
    underlying_llm_id = parse_optional_int(request.form.get("underlying_llm_id", ""))
    if prompting_technique_id is not None and not any(tech.get("id") == prompting_technique_id for tech in store.get("prompting_techniques", [])):
        return redirect("/")
    if other_technique_id is not None and not any(tech.get("id") == other_technique_id for tech in store.get("other_techniques", [])):
        return redirect("/")
    if underlying_llm_id is not None and not any(llm.get("id") == underlying_llm_id for llm in store.get("underlying_llms", [])):
        return redirect("/")

    new_effect = normalize_effect_record({
        "id": get_next_effect_id(effects),
        "description": request.form.get("description", ""),
        "evidence_rigor": request.form.get("evidence_rigor", ""),
        "solution_id": solution_id,
        "prompting_technique_id": prompting_technique_id,
        "other_technique_id": other_technique_id,
        "underlying_llm_id": underlying_llm_id,
    })
    if not new_effect.get("description"):
        return redirect("/")

    effects.append(new_effect)
    update_evidence_rigor_values(store, [new_effect])
    store["effects"] = effects
    save_data(store)
    return redirect("/")


@app.route("/edit_effect/<int:effect_id>")
def edit_effect(effect_id):
    store = load_data()
    effect = next((entry for entry in store.get("effects", []) if entry.get("id") == effect_id), None)
    if not effect:
        return redirect("/")
    return render_template(
        "edit_paper.html",
        mode="effect",
        effect=effect,
        solutions=store.get("solutions", []),
        prompting_techniques=store.get("prompting_techniques", []),
        other_techniques=store.get("other_techniques", []),
        underlying_llms=store.get("underlying_llms", []),
        evidence_rigor_values=store.get("evidence_rigor_values", DEFAULT_EVIDENCE_RIGOR_VALUES),
    )


@app.route("/update_effect/<int:effect_id>", methods=["POST"])
def update_effect(effect_id):
    store = load_data()
    effects = store.get("effects", [])
    effect = next((entry for entry in effects if entry.get("id") == effect_id), None)
    if not effect:
        return redirect("/")

    solution_id = parse_optional_int(request.form.get("solution_id", ""))
    if solution_id is not None and not any(solution.get("id") == solution_id for solution in store.get("solutions", [])):
        solution_id = None

    prompting_technique_id = parse_optional_int(request.form.get("prompting_technique_id", ""))
    other_technique_id = parse_optional_int(request.form.get("other_technique_id", ""))
    underlying_llm_id = parse_optional_int(request.form.get("underlying_llm_id", ""))
    if prompting_technique_id is not None and not any(tech.get("id") == prompting_technique_id for tech in store.get("prompting_techniques", [])):
        return redirect("/")
    if other_technique_id is not None and not any(tech.get("id") == other_technique_id for tech in store.get("other_techniques", [])):
        return redirect("/")
    if underlying_llm_id is not None and not any(llm.get("id") == underlying_llm_id for llm in store.get("underlying_llms", [])):
        return redirect("/")

    updated = normalize_effect_record({
        "id": effect_id,
        "description": request.form.get("description", effect.get("description", "")),
        "evidence_rigor": request.form.get("evidence_rigor", effect.get("evidence_rigor", "")),
        "solution_id": solution_id,
        "prompting_technique_id": prompting_technique_id,
        "other_technique_id": other_technique_id,
        "underlying_llm_id": underlying_llm_id,
    })
    if not updated.get("description"):
        return redirect("/")

    effect.update(updated)
    update_evidence_rigor_values(store, [effect])
    store["effects"] = effects
    save_data(store)
    return redirect("/")


@app.route("/delete_effect/<int:effect_id>")
def delete_effect(effect_id):
    store = load_data()
    store["effects"] = [effect for effect in store.get("effects", []) if effect.get("id") != effect_id]
    save_data(store)
    return redirect("/")


@app.route("/delete_solution/<int:solution_id>")
def delete_solution(solution_id):
    store = load_data()
    solutions = store.get("solutions", [])
    store["solutions"] = [s for s in solutions if s["id"] != solution_id]
    for effect in store.get("effects", []):
        if effect.get("solution_id") == solution_id:
            effect["solution_id"] = None
    save_data(store)
    return redirect("/")


@app.route("/delete_source/<int:source_id>")
@app.route("/delete_source/<int:solution_id>/<int:source_id>")
def delete_source(source_id, solution_id=None):
    store = load_data()
    linked_solutions = [solution for solution in store.get("solutions", []) if solution.get("source_id") == source_id]
    if linked_solutions:
        return redirect("/")
    store["sources"] = [source for source in store.get("sources", []) if source.get("id") != source_id]
    save_data(store)
    return redirect("/")


@app.route("/debug/db")
def debug_db():
    selected_env, db_url = _get_database_url_details()
    psycopg = _get_psycopg_module()
    parsed = urlparse(db_url) if db_url else None
    diagnostics = {
        "runtime": "vercel" if _is_vercel_runtime() else "local",
        "selected_env": selected_env,
        "db_url_present": bool(db_url),
        "db_scheme": (parsed.scheme if parsed else None),
        "db_host": (parsed.hostname if parsed else None),
        "db_name": ((parsed.path or "").lstrip("/") if parsed else None),
        "db_has_sslmode": ("sslmode=" in ((parsed.query or "") if parsed else "")),
        "has_psycopg": psycopg is not None,
        "connect_ok": False,
        "error": None,
    }

    if db_url and psycopg is not None:
        try:
            with psycopg.connect(db_url) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            diagnostics["connect_ok"] = True
        except Exception as exc:
            diagnostics["error"] = f"{type(exc).__name__}: {exc}"

    return diagnostics, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
