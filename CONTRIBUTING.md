# Contributing

Thanks for helping. Issues and pull requests are welcome.

* **Bug reports:** include your OS, Python version, the command you ran, and the relevant rows of `var/log.csv` (remove anything private). Never paste credentials or tokens.
* **Compatibility reports:** if it works (or not) on your unit, open an issue with the indoor unit model, Wi-Fi adapter and what you saw. This is how the Compatibility table in the README grows.
* **Code changes:** keep them small, add or update tests, and run:

  ```bash
  pip install -c constraints.txt -e ".[dev]"
  pytest
  ```

* **Charts and claims:** the README charts are an illustrative simulation (`scripts/simulate.py`, seed 42). Do not present simulated numbers as measured. If a change alters the simulation, regenerate with `python scripts/make_charts.py` and update the README numbers.

By contributing you agree that your work is released under the [MIT License](LICENSE).
