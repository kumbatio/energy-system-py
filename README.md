# kumbatio-energy (Python Conformance Port)

Zero-dependency Python 3.11+ implementation of the [Kumbatio Energy Model (`SPEC.md`)](https://github.com/kumbatio/energy-system/blob/main/SPEC.md), verified directly against all 252 conformance vectors and all 10 strategy tables in `conformance.json` (`@kumbatio/energy-system@3.0.0`).

Built for Python backends, AI agents, coordination services, and independent research tools that share one person's self-reported cognitive capacity state with JavaScript/TypeScript apps over the local interchange file (`~/.config/kumbatio/energy-state.json`).

## Install

```bash
pip install git+https://github.com/kumbatio/energy-system-py.git
```

## Quick start

```python
from kumbatio_energy import (
    load_energy_state_file,
    resolve_strategy,
)

state = load_energy_state_file()
level = state.level if state else 100

choice_budget = resolve_strategy("choice-budget", level)
autonomy = resolve_strategy("autonomy", level)
```

## Run the full conformance suite

```bash
python3 -m unittest discover -s tests -v
```

## Links

- TypeScript SDK & Specification: https://github.com/kumbatio/energy-system
- Documentation & W3C COGA guide: https://docs.kumbat.io/docs/energy-system
- Interactive demo: https://kumbat.io
