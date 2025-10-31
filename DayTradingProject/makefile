PY=python

# --- Setup ---
.PHONY: venv install
venv:
	@test -d .venv || python3.11 -m venv .venv
	@. .venv/bin/activate

install:
	. .venv/bin/activate && pip install -U pip && pip install -r requirements.txt

# --- Data & features ---
.PHONY: collect build build_plus
collect:
	. .venv/bin/activate && $(PY) features/collect.py

build:
	. .venv/bin/activate && $(PY) features/build.py

build_plus:
	. .venv/bin/activate && $(PY) features_plus/build_plus.py

# --- Models (supervised gate & RL) ---
.PHONY: train_gate train_rl
train_gate:
	. .venv/bin/activate && $(PY) models/train_gate.py

train_rl:
	. .venv/bin/activate && $(PY) rl/train_ppo.py

# --- Walk-Forward Optimization ---
.PHONY: wfo
wfo:
	. .venv/bin/activate && $(PY) wfo/walk_forward.py

# --- Run bots ---
.PHONY: paper_gate paper_rule live
paper_gate:
	. .venv/bin/activate && USE_GATE=true LOG_SUFFIX=gate $(PY) exec/paper.py

paper_rule:
	. .venv/bin/activate && USE_GATE=false LOG_SUFFIX=champion $(PY) exec/paper.py

live:
	. .venv/bin/activate && $(PY) exec/live3.py

# --- Nightly (chain steps) ---
.PHONY: nightly
nightly:
	. .venv/bin/activate && $(PY) features/collect.py && $(PY) features/build.py && $(PY) features_plus/build_plus.py && $(PY) wfo/walk_forward.py && $(PY) models/train_gate.py && $(PY) rl/train_ppo.py