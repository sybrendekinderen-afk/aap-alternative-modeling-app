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
EFFECT_POLARITY_VALUES = ["positive", "negative", "neutral"]

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


def _remote_persistence_enabled():
    if _is_vercel_runtime():
        return True
    return os.environ.get("ALLOW_REMOTE_PERSISTENCE", "").strip().lower() in {"1", "true", "yes", "on"}


def _get_psycopg_module():
    try:
        return importlib.import_module("psycopg")
    except Exception:
        return None


def _load_data_from_postgres():
    if not _remote_persistence_enabled():
        return None

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
    if not _remote_persistence_enabled():
        return False

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
    if not _remote_persistence_enabled():
        return None

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
            "modeling_tasks": [],
            "modeling_problems": [],
            "modeling_approaches": [],
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
            "modeling_tasks": data.get("modeling_tasks", data.get("modeling_purposes", [])),
            "modeling_problems": data.get("modeling_problems", []),
            "modeling_approaches": data.get("modeling_approaches", []),
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
            solution.pop("source_id", None)
            solution.pop("source_ids", None)
            solution.pop("sources", None)
            solution.pop("modeling_task_ids", None)
            solution.pop("modeling_purpose_ids", None)

            normalized_approach_ids = []
            legacy_approach_id = parse_optional_int(solution.pop("modeling_approach_id", None))
            if legacy_approach_id is not None:
                normalized_approach_ids.append(legacy_approach_id)
            for approach_id in solution.get("modeling_approach_ids", []):
                try:
                    normalized_approach_ids.append(int(approach_id))
                except (TypeError, ValueError):
                    continue
            solution["modeling_approach_ids"] = list(dict.fromkeys(normalized_approach_ids))

            normalized_other_technique_ids = []
            for technique_id in solution.get("other_technique_ids", []):
                try:
                    normalized_other_technique_ids.append(int(technique_id))
                except (TypeError, ValueError):
                    continue
            solution["other_technique_ids"] = list(dict.fromkeys(normalized_other_technique_ids))

        for modeling_task in store.get("modeling_tasks", []):
            modeling_task.pop("description", None)
            modeling_task.pop("parent_id", None)
            modeling_task.pop("root_order", None)
            model_type_id = modeling_task.get("model_type_id")
            try:
                modeling_task["model_type_id"] = int(model_type_id)
            except (TypeError, ValueError):
                modeling_task["model_type_id"] = None
            normalized_problem_ids = []
            for pid in modeling_task.get("modeling_problem_ids", []):
                try:
                    normalized_problem_ids.append(int(pid))
                except (TypeError, ValueError):
                    continue
            modeling_task["modeling_problem_ids"] = list(dict.fromkeys(normalized_problem_ids))

        for modeling_problem in store.get("modeling_problems", []):
            parent_id = modeling_problem.get("parent_id")
            modeling_problem["parent_id"] = int(parent_id) if parent_id not in [None, ""] else None
            normalized_pt_ids = []
            for ptid in modeling_problem.get("prompting_technique_ids", []):
                try:
                    normalized_pt_ids.append(int(ptid))
                except (TypeError, ValueError):
                    continue
            modeling_problem["prompting_technique_ids"] = list(dict.fromkeys(normalized_pt_ids))
            normalized_ot_ids = []
            for otid in modeling_problem.get("other_technique_ids", []):
                try:
                    normalized_ot_ids.append(int(otid))
                except (TypeError, ValueError):
                    continue
            modeling_problem["other_technique_ids"] = list(dict.fromkeys(normalized_ot_ids))
            normalized_solution_ids = []
            for sid in modeling_problem.get("solution_ids", []):
                try:
                    normalized_solution_ids.append(int(sid))
                except (TypeError, ValueError):
                    continue
            modeling_problem["solution_ids"] = list(dict.fromkeys(normalized_solution_ids))

        normalized_approaches = []
        for approach in store.get("modeling_approaches", []):
            if not isinstance(approach, dict):
                continue
            try:
                approach_id = int(approach.get("id"))
            except (TypeError, ValueError):
                approach_id = get_next_modeling_approach_id(normalized_approaches)

            normalized_solution_ids = []
            for solution_id in approach.get("solution_ids", []):
                try:
                    normalized_solution_ids.append(int(solution_id))
                except (TypeError, ValueError):
                    continue

            normalized_prompting_ids = []
            for prompting_id in approach.get("prompting_technique_ids", []):
                try:
                    normalized_prompting_ids.append(int(prompting_id))
                except (TypeError, ValueError):
                    continue

            normalized_other_ids = []
            for other_id in approach.get("other_technique_ids", []):
                try:
                    normalized_other_ids.append(int(other_id))
                except (TypeError, ValueError):
                    continue

            normalized_approaches.append({
                "id": approach_id,
                "name": str(approach.get("name", "") or "").strip(),
                "source_id": parse_optional_int(approach.get("source_id")),
                "modeling_task_id": parse_optional_int(approach.get("modeling_task_id")),
                "solution_ids": list(dict.fromkeys(normalized_solution_ids)),
                "prompting_technique_ids": list(dict.fromkeys(normalized_prompting_ids)),
                "other_technique_ids": list(dict.fromkeys(normalized_other_ids)),
            })
        store["modeling_approaches"] = normalized_approaches

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
                    "modeling_approach_id": None,
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

        for technique in store.get("prompting_techniques", []):
            technique["name"] = str(technique.get("name", "") or "").strip()
            technique["description"] = str(technique.get("description", "") or "").strip()

        for technique in store.get("other_techniques", []):
            technique["name"] = str(technique.get("name", "") or "").strip()
            technique["description"] = str(technique.get("description", "") or "").strip()

        for solution in store.get("solutions", []):
            solution["prompting_technique_ids"] = [
                tid for tid in solution.get("prompting_technique_ids", [])
                if tid not in removed_prompting_ids
            ]
            solution["other_technique_ids"] = [
                tid for tid in solution.get("other_technique_ids", [])
                if tid not in removed_other_ids
            ]

        for problem in store.get("modeling_problems", []):
            problem["prompting_technique_ids"] = [
                tid for tid in problem.get("prompting_technique_ids", [])
                if tid not in removed_prompting_ids
            ]
            problem["other_technique_ids"] = [
                tid for tid in problem.get("other_technique_ids", [])
                if tid not in removed_other_ids
            ]

        for effect in store.get("effects", []):
            if effect.get("prompting_technique_id") in removed_prompting_ids:
                effect["prompting_technique_id"] = None
            if effect.get("other_technique_id") in removed_other_ids:
                effect["other_technique_id"] = None

        valid_model_type_ids = {model_type.get("id") for model_type in store.get("model_types", [])}
        for modeling_task in store.get("modeling_tasks", []):
            if modeling_task.get("model_type_id") not in valid_model_type_ids:
                modeling_task["model_type_id"] = None

        valid_solution_ids = {solution.get("id") for solution in store.get("solutions", [])}
        valid_source_ids = {source.get("id") for source in store.get("sources", [])}
        valid_modeling_approach_ids = {approach.get("id") for approach in store.get("modeling_approaches", [])}
        valid_prompting_ids = {tech.get("id") for tech in store.get("prompting_techniques", [])}
        valid_other_ids = {tech.get("id") for tech in store.get("other_techniques", [])}
        valid_underlying_llm_ids = {llm.get("id") for llm in store.get("underlying_llms", [])}
        valid_problem_ids = {p.get("id") for p in store.get("modeling_problems", [])}
        valid_task_ids = {task.get("id") for task in store.get("modeling_tasks", [])}
        for effect in store.get("effects", []):
            effect["solution_id"] = None
            effect["prompting_technique_id"] = None
            effect["other_technique_id"] = None
            if effect.get("modeling_approach_id") not in valid_modeling_approach_ids:
                effect["modeling_approach_id"] = None
            if effect.get("underlying_llm_id") not in valid_underlying_llm_ids:
                effect["underlying_llm_id"] = None
            enforce_effect_binding_precedence(effect)

        for modeling_task in store.get("modeling_tasks", []):
            modeling_task["modeling_problem_ids"] = [
                pid for pid in modeling_task.get("modeling_problem_ids", [])
                if pid in valid_problem_ids
            ]

        for problem in store.get("modeling_problems", []):
            if problem.get("parent_id") not in valid_problem_ids:
                problem["parent_id"] = None
            problem["prompting_technique_ids"] = [
                tid for tid in problem.get("prompting_technique_ids", [])
                if tid in valid_prompting_ids
            ]
            problem["other_technique_ids"] = [
                tid for tid in problem.get("other_technique_ids", [])
                if tid in valid_other_ids
            ]
            problem["solution_ids"] = [
                sid for sid in problem.get("solution_ids", [])
                if sid in valid_solution_ids
            ]

        for approach in store.get("modeling_approaches", []):
            approach["prompting_technique_ids"] = [
                tid for tid in approach.get("prompting_technique_ids", [])
                if tid in valid_prompting_ids
            ]
            approach["other_technique_ids"] = [
                tid for tid in approach.get("other_technique_ids", [])
                if tid in valid_other_ids
            ]

        filtered_approaches = []
        for approach in store.get("modeling_approaches", []):
            if not approach.get("name"):
                continue
            if approach.get("source_id") not in valid_source_ids:
                continue
            if approach.get("modeling_task_id") not in valid_task_ids:
                continue
            filtered_approaches.append(approach)
        store["modeling_approaches"] = filtered_approaches

        valid_approach_ids = {approach.get("id") for approach in filtered_approaches}
        for solution in store.get("solutions", []):
            solution["modeling_approach_ids"] = [
                aid for aid in list(dict.fromkeys(solution.get("modeling_approach_ids", [])))
                if aid in valid_approach_ids
            ]

        approach_solution_ids = {approach.get("id"): [] for approach in filtered_approaches}
        for solution in store.get("solutions", []):
            if solution.get("id") not in valid_solution_ids:
                continue
            for aid in solution.get("modeling_approach_ids", []):
                if aid in approach_solution_ids and solution.get("id") not in approach_solution_ids[aid]:
                    approach_solution_ids[aid].append(solution.get("id"))
        for approach in filtered_approaches:
            approach["solution_ids"] = approach_solution_ids.get(approach.get("id"), [])

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
            "modeling_tasks": [],
            "modeling_problems": [],
            "modeling_approaches": [],
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
        "modeling_tasks": [],
        "modeling_problems": [],
        "modeling_approaches": [],
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


def get_modeling_task_lookup(modeling_tasks):
    return {task["id"]: task for task in modeling_tasks}


def get_modeling_task_solution_lookup(solutions):
    lookup = {}
    for solution in solutions:
        for task_id in solution.get("modeling_task_ids", []):
            lookup.setdefault(int(task_id), []).append(solution)
    return lookup


def get_modeling_problem_lookup(modeling_problems):
    return {problem["id"]: problem for problem in modeling_problems}


def get_modeling_approach_lookup(modeling_approaches):
    return {approach["id"]: approach for approach in modeling_approaches}


def get_modeling_problem_solution_lookup(solutions, modeling_problems):
    solution_lookup = {solution.get("id"): solution for solution in solutions}
    lookup = {}
    for problem in modeling_problems:
        resolved = []
        for sid in problem.get("solution_ids", []):
            solution = solution_lookup.get(sid)
            if solution:
                resolved.append(solution)
        lookup[problem.get("id")] = resolved
    return lookup


def get_solution_problem_lookup(solutions, modeling_problems):
    lookup = {solution.get("id"): [] for solution in solutions}
    for problem in modeling_problems:
        problem_id = str(problem.get("id"))
        for sid in problem.get("solution_ids", []):
            if sid in lookup:
                lookup[sid].append(problem_id)
    for sid in list(lookup.keys()):
        lookup[sid] = list(dict.fromkeys(lookup[sid]))
    return lookup


def get_solution_modeling_approach_lookup(solutions, modeling_approaches):
    solution_ids = {solution.get("id") for solution in solutions}
    lookup = {solution_id: [] for solution_id in solution_ids}
    for approach in modeling_approaches:
        for solution_id in approach.get("solution_ids", []):
            if solution_id in lookup:
                lookup[solution_id].append(approach)
    return lookup


def get_source_modeling_approach_lookup(modeling_approaches, sources):
    lookup = {source.get("id"): [] for source in sources}
    for approach in modeling_approaches:
        source_id = approach.get("source_id")
        if source_id in lookup:
            lookup[source_id].append(approach)
    return lookup


def get_next_source_id(sources):
    return max([source.get("id", 0) for source in sources], default=0) + 1


def get_next_effect_id(effects):
    return max([effect.get("id", 0) for effect in effects], default=0) + 1


def get_next_underlying_llm_id(underlying_llms):
    return max([llm.get("id", 0) for llm in underlying_llms], default=0) + 1


def get_next_modeling_approach_id(modeling_approaches):
    return max([approach.get("id", 0) for approach in modeling_approaches], default=0) + 1


def sync_solution_approach_links(store):
    modeling_approaches = store.get("modeling_approaches", [])
    solutions = store.get("solutions", [])

    valid_approach_ids = {approach.get("id") for approach in modeling_approaches}
    for solution in solutions:
        solution["modeling_approach_ids"] = [
            approach_id for approach_id in list(dict.fromkeys(solution.get("modeling_approach_ids", [])))
            if approach_id in valid_approach_ids
        ]

    for approach in modeling_approaches:
        approach["solution_ids"] = []

    approach_lookup = {approach.get("id"): approach for approach in modeling_approaches}
    for solution in solutions:
        for approach_id in solution.get("modeling_approach_ids", []):
            approach = approach_lookup.get(approach_id)
            if approach is not None and solution.get("id") not in approach["solution_ids"]:
                approach["solution_ids"].append(solution.get("id"))

    store["modeling_approaches"] = modeling_approaches
    store["solutions"] = solutions


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

    modeling_approach_id = effect.get("modeling_approach_id")
    try:
        modeling_approach_id = int(modeling_approach_id)
    except (TypeError, ValueError):
        modeling_approach_id = None

    underlying_llm_id = effect.get("underlying_llm_id")
    try:
        underlying_llm_id = int(underlying_llm_id)
    except (TypeError, ValueError):
        underlying_llm_id = None

    effect_polarity = str(effect.get("effect_polarity", "neutral") or "neutral").strip().lower()
    if effect_polarity not in EFFECT_POLARITY_VALUES:
        effect_polarity = "neutral"

    return {
        "id": effect_id,
        "description": str(effect.get("description", "") or "").strip(),
        "evidence_rigor": str(effect.get("evidence_rigor", "") or "").strip(),
        "solution_id": None,
        "modeling_approach_id": modeling_approach_id,
        "prompting_technique_id": None,
        "other_technique_id": None,
        "underlying_llm_id": underlying_llm_id,
        "effect_polarity": effect_polarity,
    }


def has_exclusive_effect_binding(solution_id, modeling_approach_id, prompting_technique_id, other_technique_id):
    selected_count = sum(
        1 for value in [solution_id, modeling_approach_id, prompting_technique_id, other_technique_id]
        if value is not None
    )
    return selected_count <= 1


def enforce_effect_binding_precedence(effect):
    effect["solution_id"] = None
    effect["prompting_technique_id"] = None
    effect["other_technique_id"] = None
    modeling_approach_id = effect.get("modeling_approach_id")

    if has_exclusive_effect_binding(None, modeling_approach_id, None, None):
        return


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
    lookup = {source.get("id"): [] for source in sources}
    for solution in solutions:
        source_id = solution.get("source_id")
        if source_id in lookup:
            lookup[source_id].append(solution)
    return lookup


def get_solution_model_type_lookup(solutions, modeling_approaches, modeling_tasks):
    task_lookup = {task.get("id"): task for task in modeling_tasks}
    approach_lookup = {approach.get("id"): approach for approach in modeling_approaches}
    lookup = {}
    for solution in solutions:
        model_type_ids = []
        for approach_id in solution.get("modeling_approach_ids", []):
            approach = approach_lookup.get(approach_id)
            if approach:
                task = task_lookup.get(approach.get("modeling_task_id"))
                if task and task.get("model_type_id") is not None:
                    model_type_ids.append(str(task["model_type_id"]))
        lookup[solution.get("id")] = list(dict.fromkeys(model_type_ids))
    return lookup


def get_solution_effect_lookup(effects):
    return {}


def get_modeling_approach_effect_lookup(effects):
    lookup = {}
    for effect in effects:
        modeling_approach_id = effect.get("modeling_approach_id")
        if modeling_approach_id is None:
            continue
        lookup.setdefault(modeling_approach_id, []).append(effect)
    return lookup


def get_solution_underlying_llm_lookup(solutions, effects):
    return {solution.get("id"): [] for solution in solutions}


def build_modeling_problem_tree(modeling_problems):
    problem_lookup = {problem["id"]: {**problem, "children": []} for problem in modeling_problems}
    roots = []

    for problem in problem_lookup.values():
        parent_id = problem.get("parent_id")
        if parent_id in problem_lookup:
            problem_lookup[parent_id]["children"].append(problem)
        else:
            roots.append(problem)

    roots.sort(key=lambda node: node.get("name", "").lower())

    def sort_children(nodes):
        nodes.sort(key=lambda node: node.get("name", "").lower())
        for node in nodes:
            sort_children(node["children"])

    for root in roots:
        sort_children(root["children"])
    return roots


def is_valid_modeling_problem_parent(problem_id, parent_id, modeling_problems):
    if not parent_id:
        return True

    if problem_id is not None and int(parent_id) == int(problem_id):
        return False

    problem_lookup = {problem["id"]: problem for problem in modeling_problems}
    current_parent_id = int(parent_id)
    while current_parent_id:
        if problem_id is not None and current_parent_id == int(problem_id):
            return False
        parent_problem = problem_lookup.get(current_parent_id)
        if not parent_problem:
            break
        current_parent_id = parent_problem.get("parent_id")
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
    modeling_tasks = store.get("modeling_tasks", [])
    modeling_problems = store.get("modeling_problems", [])
    modeling_approaches = store.get("modeling_approaches", [])
    return render_template(
        "index.html",
        solutions=solutions,
        sources=sources,
        effects=effects,
        underlying_llms=underlying_llms,
        solution_effect_lookup=get_solution_effect_lookup(effects),
        modeling_approach_effect_lookup=get_modeling_approach_effect_lookup(effects),
        solution_underlying_llm_lookup=get_solution_underlying_llm_lookup(solutions, effects),
        source_modeling_approach_lookup=get_source_modeling_approach_lookup(modeling_approaches, sources),
        solution_model_type_lookup=get_solution_model_type_lookup(solutions, modeling_approaches, modeling_tasks),
        solution_problem_lookup=get_solution_problem_lookup(solutions, modeling_problems),
        solution_modeling_approach_lookup=get_solution_modeling_approach_lookup(solutions, modeling_approaches),
        model_types=model_types,
        model_type_lookup=get_model_type_lookup(model_types),
        prompting_techniques=prompting_techniques,
        prompting_technique_lookup=get_prompting_technique_lookup(prompting_techniques),
        other_techniques=other_techniques,
        other_technique_lookup=get_other_technique_lookup(other_techniques),
        underlying_llm_lookup=get_underlying_llm_lookup(underlying_llms),
        modeling_tasks=modeling_tasks,
        modeling_task_lookup=get_modeling_task_lookup(modeling_tasks),
        modeling_task_solution_lookup=get_modeling_task_solution_lookup(solutions),
        modeling_problems=modeling_problems,
        modeling_problem_lookup=get_modeling_problem_lookup(modeling_problems),
        modeling_approaches=modeling_approaches,
        modeling_approach_lookup=get_modeling_approach_lookup(modeling_approaches),
        modeling_problem_tree=build_modeling_problem_tree(modeling_problems),
        modeling_problem_solution_lookup=get_modeling_problem_solution_lookup(solutions, modeling_problems),
        evidence_rigor_values=store.get("evidence_rigor_values", DEFAULT_EVIDENCE_RIGOR_VALUES)
    )


@app.route("/add_solution", methods=["POST"])
def add_solution():
    store = load_data()
    solutions = store.get("solutions", [])
    modeling_approaches = store.get("modeling_approaches", [])
    modeling_problems = store.get("modeling_problems", [])

    next_id = max([s["id"] for s in solutions], default=0) + 1

    valid_approach_ids = {approach.get("id") for approach in modeling_approaches}
    modeling_approach_ids = [
        aid for aid in list(dict.fromkeys(parse_int_list(request.form.getlist("modeling_approach_ids"))))
        if aid in valid_approach_ids
    ]

    prompting_technique_ids = parse_int_list(request.form.getlist("prompting_technique_ids"))
    other_technique_ids = parse_int_list(request.form.getlist("other_technique_ids"))
    valid_problem_ids = {problem.get("id") for problem in modeling_problems}
    modeling_problem_ids = [
        pid for pid in parse_int_list(request.form.getlist("modeling_problem_ids"))
        if pid in valid_problem_ids
    ]

    solutions.append({
        "id": next_id,
        "name": request.form["name"],
        "prompting_technique_ids": prompting_technique_ids,
        "other_technique_ids": other_technique_ids,
        "justification": request.form.get("justification", ""),
        "modeling_approach_ids": modeling_approach_ids,
    })

    for problem in modeling_problems:
        existing_solution_ids = list(dict.fromkeys(parse_int_list(problem.get("solution_ids", []))))
        if problem.get("id") in modeling_problem_ids:
            if next_id not in existing_solution_ids:
                existing_solution_ids.append(next_id)
        problem["solution_ids"] = existing_solution_ids

    store["solutions"] = solutions
    store["modeling_problems"] = modeling_problems
    sync_solution_approach_links(store)
    save_data(store)
    return redirect("/")


@app.route("/add_modeling_approach", methods=["POST"])
def add_modeling_approach():
    store = load_data()
    modeling_approaches = store.get("modeling_approaches", [])
    sources = store.get("sources", [])
    modeling_tasks = store.get("modeling_tasks", [])
    solutions = store.get("solutions", [])
    prompting_techniques = store.get("prompting_techniques", [])
    other_techniques = store.get("other_techniques", [])
    modeling_problems = store.get("modeling_problems", [])

    name = request.form.get("modeling_approach_name", "").strip()
    source_id = parse_optional_int(request.form.get("source_id", ""))
    modeling_task_id = parse_optional_int(request.form.get("modeling_task_id", ""))
    solution_ids = list(dict.fromkeys(parse_int_list(request.form.getlist("solution_ids"))))
    prompting_technique_ids = list(dict.fromkeys(parse_int_list(request.form.getlist("prompting_technique_ids"))))
    other_technique_ids = list(dict.fromkeys(parse_int_list(request.form.getlist("other_technique_ids"))))
    modeling_problem_ids = list(dict.fromkeys(parse_int_list(request.form.getlist("modeling_problem_ids"))))

    if not name:
        return redirect("/")
    if source_id is None or not any(source.get("id") == source_id for source in sources):
        return redirect("/")
    if modeling_task_id is None or not any(task.get("id") == modeling_task_id for task in modeling_tasks):
        return redirect("/")
    valid_solution_ids = {solution.get("id") for solution in solutions}
    valid_prompting_ids = {tech.get("id") for tech in prompting_techniques}
    valid_other_ids = {tech.get("id") for tech in other_techniques}
    prompting_technique_ids = [tid for tid in prompting_technique_ids if tid in valid_prompting_ids]
    other_technique_ids = [tid for tid in other_technique_ids if tid in valid_other_ids]
    solution_ids = [sid for sid in solution_ids if sid in valid_solution_ids]
    valid_problem_ids = {problem.get("id") for problem in modeling_problems}
    modeling_problem_ids = [pid for pid in modeling_problem_ids if pid in valid_problem_ids]

    new_id = get_next_modeling_approach_id(modeling_approaches)
    modeling_approaches.append({
        "id": new_id,
        "name": name,
        "source_id": source_id,
        "modeling_task_id": modeling_task_id,
        "solution_ids": [],
        "prompting_technique_ids": prompting_technique_ids,
        "other_technique_ids": other_technique_ids,
        "modeling_problem_ids": modeling_problem_ids,
    })

    for solution in solutions:
        if solution.get("id") in solution_ids:
            existing_ids = list(dict.fromkeys(solution.get("modeling_approach_ids", [])))
            if new_id not in existing_ids:
                existing_ids.append(new_id)
            solution["modeling_approach_ids"] = existing_ids

    store["modeling_approaches"] = modeling_approaches
    store["solutions"] = solutions
    sync_solution_approach_links(store)
    save_data(store)
    return redirect("/")


@app.route("/update_modeling_approach/<int:modeling_approach_id>", methods=["POST"])
def update_modeling_approach(modeling_approach_id):
    store = load_data()
    modeling_approaches = store.get("modeling_approaches", [])
    sources = store.get("sources", [])
    modeling_tasks = store.get("modeling_tasks", [])
    solutions = store.get("solutions", [])
    prompting_techniques = store.get("prompting_techniques", [])
    other_techniques = store.get("other_techniques", [])
    modeling_problems = store.get("modeling_problems", [])

    source_id = parse_optional_int(request.form.get("source_id", ""))
    modeling_task_id = parse_optional_int(request.form.get("modeling_task_id", ""))
    solution_ids = list(dict.fromkeys(parse_int_list(request.form.getlist("solution_ids"))))
    prompting_technique_ids = list(dict.fromkeys(parse_int_list(request.form.getlist("prompting_technique_ids"))))
    other_technique_ids = list(dict.fromkeys(parse_int_list(request.form.getlist("other_technique_ids"))))
    modeling_problem_ids = list(dict.fromkeys(parse_int_list(request.form.getlist("modeling_problem_ids"))))
    valid_solution_ids = {solution.get("id") for solution in solutions}
    valid_prompting_ids = {tech.get("id") for tech in prompting_techniques}
    valid_other_ids = {tech.get("id") for tech in other_techniques}
    valid_problem_ids = {problem.get("id") for problem in modeling_problems}
    solution_ids = [sid for sid in solution_ids if sid in valid_solution_ids]
    prompting_technique_ids = [tid for tid in prompting_technique_ids if tid in valid_prompting_ids]
    other_technique_ids = [tid for tid in other_technique_ids if tid in valid_other_ids]
    modeling_problem_ids = [pid for pid in modeling_problem_ids if pid in valid_problem_ids]

    for approach in modeling_approaches:
        if approach.get("id") != modeling_approach_id:
            continue

        name = request.form.get("modeling_approach_name", approach.get("name", "")).strip()
        if not name:
            return redirect("/")
        if source_id is None or not any(source.get("id") == source_id for source in sources):
            return redirect("/")
        if modeling_task_id is None or not any(task.get("id") == modeling_task_id for task in modeling_tasks):
            return redirect("/")

        approach["name"] = name
        approach["source_id"] = source_id
        approach["modeling_task_id"] = modeling_task_id
        approach["prompting_technique_ids"] = prompting_technique_ids
        approach["other_technique_ids"] = other_technique_ids
        approach["modeling_problem_ids"] = modeling_problem_ids

        for solution in solutions:
            existing_ids = [
                aid for aid in solution.get("modeling_approach_ids", [])
                if aid != modeling_approach_id
            ]
            if solution.get("id") in solution_ids:
                existing_ids.append(modeling_approach_id)
            solution["modeling_approach_ids"] = list(dict.fromkeys(existing_ids))
        break

    store["modeling_approaches"] = modeling_approaches
    store["solutions"] = solutions
    sync_solution_approach_links(store)
    save_data(store)
    return redirect("/")


@app.route("/delete_modeling_approach/<int:modeling_approach_id>")
def delete_modeling_approach(modeling_approach_id):
    store = load_data()
    linked_solutions = [
        solution for solution in store.get("solutions", [])
        if modeling_approach_id in solution.get("modeling_approach_ids", [])
    ]
    if linked_solutions:
        return redirect("/")
    for solution in store.get("solutions", []):
        solution["modeling_approach_ids"] = [
            aid for aid in solution.get("modeling_approach_ids", [])
            if aid != modeling_approach_id
        ]
    for effect in store.get("effects", []):
        if effect.get("modeling_approach_id") == modeling_approach_id:
            effect["modeling_approach_id"] = None
    for approach in store.get("modeling_approaches", []):
        if approach.get("id") == modeling_approach_id:
            approach["prompting_technique_ids"] = list(dict.fromkeys(parse_int_list(approach.get("prompting_technique_ids", []))))
            approach["other_technique_ids"] = list(dict.fromkeys(parse_int_list(approach.get("other_technique_ids", []))))
    store["modeling_approaches"] = [
        approach for approach in store.get("modeling_approaches", [])
        if approach.get("id") != modeling_approach_id
    ]
    sync_solution_approach_links(store)
    save_data(store)
    return redirect("/")


@app.route("/add_modeling_task", methods=["POST"])
def add_modeling_task():
    store = load_data()
    name = request.form.get("modeling_task_name", "").strip()
    model_type_id = parse_optional_int(request.form.get("model_type_id", ""))
    modeling_problems = store.get("modeling_problems", [])
    valid_problem_ids = {problem.get("id") for problem in modeling_problems}
    modeling_problem_ids = [
        pid for pid in parse_int_list(request.form.getlist("modeling_problem_ids"))
        if pid in valid_problem_ids
    ]

    if name:
        modeling_tasks = store.get("modeling_tasks", [])
        modeling_tasks.append({
            "id": max([mt["id"] for mt in modeling_tasks], default=0) + 1,
            "name": name,
            "model_type_id": model_type_id,
            "modeling_problem_ids": list(dict.fromkeys(modeling_problem_ids))
        })
        store["modeling_tasks"] = modeling_tasks
        save_data(store)

    return redirect("/")


@app.route("/update_modeling_task/<int:modeling_task_id>", methods=["POST"])
def update_modeling_task(modeling_task_id):
    store = load_data()
    modeling_tasks = store.get("modeling_tasks", [])
    modeling_problems = store.get("modeling_problems", [])
    valid_problem_ids = {p["id"] for p in modeling_problems}
    model_type_value = parse_optional_int(request.form.get("model_type_id", ""))
    modeling_problem_ids = [
        pid for pid in parse_int_list(request.form.getlist("modeling_problem_ids"))
        if pid in valid_problem_ids
    ]

    for modeling_task in modeling_tasks:
        if modeling_task["id"] == modeling_task_id:
            modeling_task["name"] = request.form.get("modeling_task_name", modeling_task["name"]).strip()
            modeling_task["model_type_id"] = model_type_value
            modeling_task["modeling_problem_ids"] = list(dict.fromkeys(modeling_problem_ids))
            break

    store["modeling_tasks"] = modeling_tasks
    save_data(store)
    return redirect("/")


@app.route("/delete_modeling_task/<int:modeling_task_id>")
def delete_modeling_task(modeling_task_id):
    store = load_data()
    linked_approaches = [
        approach for approach in store.get("modeling_approaches", [])
        if approach.get("modeling_task_id") == modeling_task_id
    ]
    if linked_approaches:
        return redirect("/")
    removed_approach_ids = {
        approach.get("id")
        for approach in store.get("modeling_approaches", [])
        if approach.get("modeling_task_id") == modeling_task_id
    }
    store["modeling_tasks"] = [
        mt for mt in store.get("modeling_tasks", [])
        if mt["id"] != modeling_task_id
    ]
    for solution in store.get("solutions", []):
        solution["modeling_task_ids"] = [
            tid for tid in solution.get("modeling_task_ids", [])
            if tid != modeling_task_id
        ]
    store["modeling_approaches"] = [
        approach for approach in store.get("modeling_approaches", [])
        if approach.get("modeling_task_id") != modeling_task_id
    ]
    for solution in store.get("solutions", []):
        solution["modeling_approach_ids"] = [
            aid for aid in solution.get("modeling_approach_ids", [])
            if aid not in removed_approach_ids
        ]
    sync_solution_approach_links(store)
    save_data(store)
    return redirect("/")


@app.route("/add_modeling_problem", methods=["POST"])
def add_modeling_problem():
    store = load_data()
    name = request.form.get("modeling_problem_name", "").strip()
    parent_id = parse_optional_int(request.form.get("parent_id", ""))
    prompting_technique_ids = parse_int_list(request.form.getlist("prompting_technique_ids"))
    other_technique_ids = parse_int_list(request.form.getlist("other_technique_ids"))
    valid_solution_ids = {solution.get("id") for solution in store.get("solutions", [])}
    solution_ids = [
        sid for sid in parse_int_list(request.form.getlist("solution_ids"))
        if sid in valid_solution_ids
    ]

    if name:
        modeling_problems = store.get("modeling_problems", [])
        if is_valid_modeling_problem_parent(None, parent_id, modeling_problems):
            modeling_problems.append({
                "id": max([p["id"] for p in modeling_problems], default=0) + 1,
                "name": name,
                "parent_id": parent_id,
                "prompting_technique_ids": prompting_technique_ids,
                "other_technique_ids": other_technique_ids,
                "solution_ids": list(dict.fromkeys(solution_ids)),
            })
            store["modeling_problems"] = modeling_problems
            save_data(store)

    return redirect("/")


@app.route("/update_modeling_problem/<int:modeling_problem_id>", methods=["POST"])
def update_modeling_problem(modeling_problem_id):
    store = load_data()
    modeling_problems = store.get("modeling_problems", [])
    parent_value = parse_optional_int(request.form.get("parent_id", ""))
    prompting_technique_ids = parse_int_list(request.form.getlist("prompting_technique_ids"))
    other_technique_ids = parse_int_list(request.form.getlist("other_technique_ids"))
    valid_solution_ids = {solution.get("id") for solution in store.get("solutions", [])}
    solution_ids = [
        sid for sid in parse_int_list(request.form.getlist("solution_ids"))
        if sid in valid_solution_ids
    ]

    for modeling_problem in modeling_problems:
        if modeling_problem["id"] == modeling_problem_id:
            modeling_problem["name"] = request.form.get("modeling_problem_name", modeling_problem["name"]).strip()
            if is_valid_modeling_problem_parent(modeling_problem_id, parent_value, modeling_problems):
                modeling_problem["parent_id"] = parent_value
            modeling_problem["prompting_technique_ids"] = prompting_technique_ids
            modeling_problem["other_technique_ids"] = other_technique_ids
            modeling_problem["solution_ids"] = list(dict.fromkeys(solution_ids))
            break

    store["modeling_problems"] = modeling_problems
    save_data(store)
    return redirect("/")


@app.route("/delete_modeling_problem/<int:modeling_problem_id>")
def delete_modeling_problem(modeling_problem_id):
    store = load_data()
    modeling_problems = store.get("modeling_problems", [])
    store["modeling_problems"] = []
    for problem in modeling_problems:
        if problem["id"] == modeling_problem_id:
            continue
        if problem.get("parent_id") == modeling_problem_id:
            problem["parent_id"] = None
        store["modeling_problems"].append(problem)

    for modeling_task in store.get("modeling_tasks", []):
        modeling_task["modeling_problem_ids"] = [
            pid for pid in modeling_task.get("modeling_problem_ids", [])
            if pid != modeling_problem_id
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
    description = request.form.get("prompting_technique_description", "").strip()
    if name:
        techniques = store.get("prompting_techniques", [])
        existing = next((pt for pt in techniques if pt.get("name") == name), None)
        if not existing:
            techniques.append({
                "id": max([pt["id"] for pt in techniques], default=0) + 1,
                "name": name,
                "description": description,
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
            technique["description"] = request.form.get("prompting_technique_description", technique.get("description", "")).strip()
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

    for approach in store.get("modeling_approaches", []):
        approach["prompting_technique_ids"] = [
            pid for pid in approach.get("prompting_technique_ids", [])
            if pid != prompting_technique_id
        ]

    save_data(store)
    return redirect("/")


@app.route("/add_other_technique", methods=["POST"])
def add_other_technique():
    store = load_data()
    name = request.form.get("other_technique_name", "").strip()
    description = request.form.get("other_technique_description", "").strip()
    if name:
        techniques = store.get("other_techniques", [])
        existing = next((tech for tech in techniques if tech.get("name") == name), None)
        if not existing:
            techniques.append({
                "id": max([tech["id"] for tech in techniques], default=0) + 1,
                "name": name,
                "description": description,
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
            technique["description"] = request.form.get("other_technique_description", technique.get("description", "")).strip()
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

    for approach in store.get("modeling_approaches", []):
        approach["other_technique_ids"] = [
            tid for tid in approach.get("other_technique_ids", [])
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
    modeling_problems = store.get("modeling_problems", [])
    selected_modeling_problem_ids = [
        problem.get("id")
        for problem in modeling_problems
        if solution_id in problem.get("solution_ids", [])
    ]
    return render_template(
        "edit_paper.html",
        solution=solution,
        mode="solution",
        sources=store.get("sources", []),
        modeling_approaches=store.get("modeling_approaches", []),
        prompting_techniques=store.get("prompting_techniques", []),
        other_techniques=store.get("other_techniques", []),
        modeling_problems=modeling_problems,
        selected_modeling_problem_ids=selected_modeling_problem_ids,
    )


@app.route("/edit_modeling_approach/<int:modeling_approach_id>")
def edit_modeling_approach(modeling_approach_id):
    store = load_data()
    modeling_approach = next(
        (entry for entry in store.get("modeling_approaches", []) if entry.get("id") == modeling_approach_id),
        None,
    )
    if not modeling_approach:
        return redirect("/")

    return render_template(
        "edit_paper.html",
        mode="modeling_approach",
        modeling_approach=modeling_approach,
        sources=store.get("sources", []),
        modeling_tasks=store.get("modeling_tasks", []),
        solutions=store.get("solutions", []),
        prompting_techniques=store.get("prompting_techniques", []),
        other_techniques=store.get("other_techniques", []),
    )


@app.route("/update_solution/<int:solution_id>", methods=["POST"])
def update_solution(solution_id):
    store = load_data()
    solution = find_solution(store.get("solutions", []), solution_id)
    if solution:
        solution["name"] = request.form.get("name", solution["name"])
        valid_approach_ids = {approach.get("id") for approach in store.get("modeling_approaches", [])}
        modeling_approach_ids = [
            aid for aid in list(dict.fromkeys(parse_int_list(request.form.getlist("modeling_approach_ids"))))
            if aid in valid_approach_ids
        ]
        prompting_technique_ids = parse_int_list(request.form.getlist("prompting_technique_ids"))
        other_technique_ids = parse_int_list(request.form.getlist("other_technique_ids"))
        modeling_problems = store.get("modeling_problems", [])
        valid_problem_ids = {problem.get("id") for problem in modeling_problems}
        modeling_problem_ids = [
            pid for pid in parse_int_list(request.form.getlist("modeling_problem_ids"))
            if pid in valid_problem_ids
        ]
        solution["modeling_approach_ids"] = modeling_approach_ids
        solution["prompting_technique_ids"] = prompting_technique_ids
        solution["other_technique_ids"] = other_technique_ids
        solution["justification"] = request.form.get("justification", solution["justification"])

        selected_problem_ids = set(modeling_problem_ids)
        for problem in modeling_problems:
            existing_solution_ids = list(dict.fromkeys(parse_int_list(problem.get("solution_ids", []))))
            if problem.get("id") in selected_problem_ids:
                if solution_id not in existing_solution_ids:
                    existing_solution_ids.append(solution_id)
            else:
                existing_solution_ids = [sid for sid in existing_solution_ids if sid != solution_id]
            problem["solution_ids"] = existing_solution_ids

        store["modeling_problems"] = modeling_problems
        sync_solution_approach_links(store)
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

    modeling_approach_id = parse_optional_int(request.form.get("modeling_approach_id", ""))
    if modeling_approach_id is not None and not any(approach.get("id") == modeling_approach_id for approach in store.get("modeling_approaches", [])):
        modeling_approach_id = None

    underlying_llm_id = parse_optional_int(request.form.get("underlying_llm_id", ""))
    if underlying_llm_id is not None and not any(llm.get("id") == underlying_llm_id for llm in store.get("underlying_llms", [])):
        return redirect("/")
    if not has_exclusive_effect_binding(None, modeling_approach_id, None, None):
        return redirect("/")

    new_effect = normalize_effect_record({
        "id": get_next_effect_id(effects),
        "description": request.form.get("description", ""),
        "evidence_rigor": request.form.get("evidence_rigor", ""),
        "effect_polarity": request.form.get("effect_polarity", "neutral"),
        "solution_id": None,
        "modeling_approach_id": modeling_approach_id,
        "prompting_technique_id": None,
        "other_technique_id": None,
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
        modeling_approaches=store.get("modeling_approaches", []),
        prompting_techniques=store.get("prompting_techniques", []),
        other_techniques=store.get("other_techniques", []),
        underlying_llms=store.get("underlying_llms", []),
        effect_polarity_values=EFFECT_POLARITY_VALUES,
        evidence_rigor_values=store.get("evidence_rigor_values", DEFAULT_EVIDENCE_RIGOR_VALUES),
    )


@app.route("/update_effect/<int:effect_id>", methods=["POST"])
def update_effect(effect_id):
    store = load_data()
    effects = store.get("effects", [])
    effect = next((entry for entry in effects if entry.get("id") == effect_id), None)
    if not effect:
        return redirect("/")

    modeling_approach_id = parse_optional_int(request.form.get("modeling_approach_id", ""))
    if modeling_approach_id is not None and not any(approach.get("id") == modeling_approach_id for approach in store.get("modeling_approaches", [])):
        modeling_approach_id = None

    underlying_llm_id = parse_optional_int(request.form.get("underlying_llm_id", ""))
    if underlying_llm_id is not None and not any(llm.get("id") == underlying_llm_id for llm in store.get("underlying_llms", [])):
        return redirect("/")
    if not has_exclusive_effect_binding(None, modeling_approach_id, None, None):
        return redirect("/")

    updated = normalize_effect_record({
        "id": effect_id,
        "description": request.form.get("description", effect.get("description", "")),
        "evidence_rigor": request.form.get("evidence_rigor", effect.get("evidence_rigor", "")),
        "effect_polarity": request.form.get("effect_polarity", effect.get("effect_polarity", "neutral")),
        "solution_id": None,
        "modeling_approach_id": modeling_approach_id,
        "prompting_technique_id": None,
        "other_technique_id": None,
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
    for problem in store.get("modeling_problems", []):
        problem["solution_ids"] = [
            sid for sid in problem.get("solution_ids", [])
            if sid != solution_id
        ]
    sync_solution_approach_links(store)
    save_data(store)
    return redirect("/")


@app.route("/delete_source/<int:source_id>")
@app.route("/delete_source/<int:solution_id>/<int:source_id>")
def delete_source(source_id, solution_id=None):
    store = load_data()
    linked_approaches = [
        approach for approach in store.get("modeling_approaches", [])
        if approach.get("source_id") == source_id
    ]
    if linked_approaches:
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
