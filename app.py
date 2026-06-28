from flask import Flask, render_template, request, redirect
import base64
import json
import os

import requests as http_requests

app = Flask(__name__)

DATA_FILE = os.path.join(os.path.dirname(__file__), "data.json")
DEFAULT_EVIDENCE_RIGOR_VALUES = [
    "quantitative ablation",
    "case based isolation",
    "bundled comparison",
    "momolitic application",
]

_BLOB_STORE = "app-data"
_BLOB_KEY = "store"


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
    """Return parsed JSON from Netlify Blobs or the local data file."""
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
        return {"solutions": [], "model_types": [], "prompting_techniques": [], "modeling_purposes": [], "evidence_rigor_values": DEFAULT_EVIDENCE_RIGOR_VALUES}

    if isinstance(data, dict):
        store = {
            "solutions": data.get("solutions", []),
            "model_types": data.get("model_types", []),
            "prompting_techniques": data.get("prompting_techniques", []),
            "modeling_purposes": data.get("modeling_purposes", []),
            "evidence_rigor_values": data.get("evidence_rigor_values", DEFAULT_EVIDENCE_RIGOR_VALUES)
        }

        for solution in store.get("solutions", []):
            for source in solution.get("sources", []):
                if "effects" not in source:
                    old_effect = source.get("effect")
                    if isinstance(old_effect, dict):
                        source["effects"] = [{
                            "description": old_effect.get("description", ""),
                            "evidence_rigor": old_effect.get("evidence_rigor", "")
                        }]
                    else:
                        source["effects"] = []
                normalized_effects = []
                for effect in source.get("effects", []):
                    if not isinstance(effect, dict):
                        continue
                    description = str(effect.get("description", "") or "").strip()
                    legacy_name = str(effect.get("name", "") or "").strip()
                    evidence_rigor = str(effect.get("evidence_rigor", "") or "").strip()
                    if not description and legacy_name:
                        description = legacy_name
                    if description or evidence_rigor:
                        normalized_effects.append({
                            "description": description,
                            "evidence_rigor": evidence_rigor
                        })
                source["effects"] = normalized_effects
                source.pop("effect", None)
                source.pop("effect_ids", None)
                source.pop("link", None)

        for modeling_purpose in store.get("modeling_purposes", []):
            modeling_purpose.pop("description", None)
            parent_id = modeling_purpose.get("parent_id")
            modeling_purpose["parent_id"] = int(parent_id) if parent_id not in [None, ""] else None

        normalize_root_purpose_order(store.get("modeling_purposes", []))

        evidence_values = list(store.get("evidence_rigor_values", []))
        for solution in store.get("solutions", []):
            for source in solution.get("sources", []):
                for effect in source.get("effects", []):
                    value = effect.get("evidence_rigor", "").strip()
                    if value:
                        evidence_values.append(value)
        store["evidence_rigor_values"] = list(dict.fromkeys([value for value in evidence_values if value]))

        return store

    if isinstance(data, list):
        return {"solutions": data, "model_types": [], "prompting_techniques": [], "modeling_purposes": [], "evidence_rigor_values": DEFAULT_EVIDENCE_RIGOR_VALUES}

    return {"solutions": [], "model_types": [], "prompting_techniques": [], "modeling_purposes": [], "evidence_rigor_values": DEFAULT_EVIDENCE_RIGOR_VALUES}


def save_data(data):
    ctx = _get_blob_context()
    if ctx:
        http_requests.put(
            _blob_url(ctx),
            data=json.dumps(data),
            headers={
                "Authorization": f"Bearer {ctx['token']}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
    else:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


def find_solution(solutions, solution_id):
    for solution in solutions:
        if solution["id"] == solution_id:
            return solution
    return None


def get_model_type_lookup(model_types):
    return {model_type["id"]: model_type for model_type in model_types}


def get_prompting_technique_lookup(prompting_techniques):
    return {technique["id"]: technique for technique in prompting_techniques}


def get_modeling_purpose_lookup(modeling_purposes):
    return {purpose["id"]: purpose for purpose in modeling_purposes}


def get_modeling_purpose_solution_lookup(solutions):
    lookup = {}
    for solution in solutions:
        for purpose_id in solution.get("modeling_purpose_ids", []):
            purpose_key = int(purpose_id)
            lookup.setdefault(purpose_key, []).append(solution)
    return lookup


def get_solution_model_type_lookup(solutions):
    lookup = {}
    for solution in solutions:
        model_type_ids = []
        for source in solution.get("sources", []):
            model_type_id = source.get("model_type_id")
            if model_type_id in [None, ""]:
                continue
            model_type_ids.append(str(model_type_id))
        lookup[solution.get("id")] = list(dict.fromkeys(model_type_ids))
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


def parse_effects(raw_text):
    effects = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) == 1:
            description, evidence_rigor = parts[0], ""
        else:
            description, evidence_rigor = parts[0], parts[1]
        if description or evidence_rigor:
            effects.append({
                "description": description,
                "evidence_rigor": evidence_rigor
            })
    return effects


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
    model_types = store.get("model_types", [])
    prompting_techniques = store.get("prompting_techniques", [])
    modeling_purposes = store.get("modeling_purposes", [])
    return render_template(
        "index.html",
        solutions=solutions,
        solution_model_type_lookup=get_solution_model_type_lookup(solutions),
        model_types=model_types,
        model_type_lookup=get_model_type_lookup(model_types),
        prompting_techniques=prompting_techniques,
        prompting_technique_lookup=get_prompting_technique_lookup(prompting_techniques),
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

    next_id = max([s["id"] for s in solutions], default=0) + 1

    prompting_technique_ids = request.form.getlist("prompting_technique_ids")
    modeling_purpose_ids = request.form.getlist("modeling_purpose_ids")

    solutions.append({
        "id": next_id,
        "name": request.form["name"],
        "prompting_technique_ids": [int(pid) for pid in prompting_technique_ids if pid],
        "modeling_purpose_ids": [int(pid) for pid in modeling_purpose_ids if pid],
        "justification": request.form.get("justification", ""),
        "sources": []
    })

    store["solutions"] = solutions
    save_data(store)
    return redirect("/")


@app.route("/add_modeling_purpose", methods=["POST"])
def add_modeling_purpose():
    store = load_data()
    name = request.form.get("modeling_purpose_name", "").strip()
    parent_id = request.form.get("parent_id", "").strip()

    if name:
        modeling_purposes = store.get("modeling_purposes", [])
        parent_value = int(parent_id) if parent_id else None
        if is_valid_modeling_purpose_parent(None, parent_value, modeling_purposes):
            new_root_order = len([mp for mp in modeling_purposes if mp.get("parent_id") is None]) if parent_value is None else None
            modeling_purposes.append({
                "id": max([mp["id"] for mp in modeling_purposes], default=0) + 1,
                "name": name,
                "parent_id": parent_value,
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
    parent_id = request.form.get("parent_id", "").strip()
    parent_value = int(parent_id) if parent_id else None

    for modeling_purpose in modeling_purposes:
        if modeling_purpose["id"] == modeling_purpose_id:
            previous_parent_id = modeling_purpose.get("parent_id")
            modeling_purpose["name"] = request.form.get("modeling_purpose_name", modeling_purpose["name"]).strip()
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

    for solution in store.get("solutions", []):
        solution["sources"] = [
            source for source in solution.get("sources", [])
            if source.get("model_type_id") != model_type_id
        ]

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
        prompting_techniques=store.get("prompting_techniques", []),
        modeling_purposes=store.get("modeling_purposes", [])
    )


@app.route("/update_solution/<int:solution_id>", methods=["POST"])
def update_solution(solution_id):
    store = load_data()
    solution = find_solution(store.get("solutions", []), solution_id)
    if solution:
        solution["name"] = request.form.get("name", solution["name"])
        prompting_technique_ids = request.form.getlist("prompting_technique_ids")
        modeling_purpose_ids = request.form.getlist("modeling_purpose_ids")
        solution["prompting_technique_ids"] = [int(pid) for pid in prompting_technique_ids if pid]
        solution["modeling_purpose_ids"] = [int(pid) for pid in modeling_purpose_ids if pid]
        solution["justification"] = request.form.get("justification", solution["justification"])
        save_data(store)
    return redirect("/")


@app.route("/add_source/<int:solution_id>", methods=["POST"])
def add_source(solution_id):
    store = load_data()
    solutions = store.get("solutions", [])
    model_types = store.get("model_types", [])

    model_type_id = request.form.get("model_type_id", "").strip()
    effects_text = request.form.get("effects_text", "")

    resolved_model_type_value = resolve_model_type_value(model_type_id, model_types)

    for solution in solutions:
        if solution["id"] == solution_id:
            parsed_effects = parse_effects(effects_text)
            update_evidence_rigor_values(store, parsed_effects)
            solution["sources"].append({
                "id": len(solution["sources"]) + 1,
                "title": request.form.get("title", ""),
                "author": request.form.get("author", ""),
                "doi": request.form.get("doi", ""),
                "model_type_id": resolved_model_type_value,
                "effects": parsed_effects
            })
            break

    store["solutions"] = solutions
    store["model_types"] = model_types
    save_data(store)
    return redirect("/")


@app.route("/edit_source/<int:solution_id>/<int:source_id>")
def edit_source(solution_id, source_id):
    store = load_data()
    solution = find_solution(store.get("solutions", []), solution_id)
    if not solution:
        return redirect("/")

    source = next((s for s in solution.get("sources", []) if s["id"] == source_id), None)
    if not source:
        return redirect("/")

    return render_template(
        "edit_paper.html",
        solution=solution,
        source=source,
        mode="source",
        model_types=store.get("model_types", []),
        model_type_lookup=get_model_type_lookup(store.get("model_types", [])),
        evidence_rigor_values=store.get("evidence_rigor_values", DEFAULT_EVIDENCE_RIGOR_VALUES)
    )


@app.route("/update_source/<int:solution_id>/<int:source_id>", methods=["POST"])
def update_source(solution_id, source_id):
    store = load_data()
    solution = find_solution(store.get("solutions", []), solution_id)
    if solution:
        solutions = store.get("solutions", [])
        model_types = store.get("model_types", [])
        for source in solution.get("sources", []):
            if source["id"] == source_id:
                source["title"] = request.form.get("title", source["title"])
                source["author"] = request.form.get("author", source["author"])
                source["doi"] = request.form.get("doi", source["doi"])
                source.pop("link", None)

                model_type_id = request.form.get("model_type_id", "").strip()
                resolved_model_type_value = resolve_model_type_value(model_type_id, model_types)
                source["model_type_id"] = resolved_model_type_value

                parsed_effects = parse_effects(request.form.get("effects_text", ""))
                update_evidence_rigor_values(store, parsed_effects)
                source["effects"] = parsed_effects
                break

        store["solutions"] = solutions
        store["model_types"] = model_types
        save_data(store)
    return redirect("/")


@app.route("/delete_solution/<int:solution_id>")
def delete_solution(solution_id):
    store = load_data()
    solutions = store.get("solutions", [])
    store["solutions"] = [s for s in solutions if s["id"] != solution_id]
    save_data(store)
    return redirect("/")


@app.route("/delete_source/<int:solution_id>/<int:source_id>")
def delete_source(solution_id, source_id):
    store = load_data()
    solutions = store.get("solutions", [])

    for solution in solutions:
        if solution["id"] == solution_id:
            solution["sources"] = [s for s in solution["sources"] if s["id"] != source_id]
            break

    store["solutions"] = solutions
    save_data(store)
    return redirect("/")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
