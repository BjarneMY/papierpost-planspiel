import os, runpy
os.environ["PLANSPIEL_DB"] = "planspiel_gruppe2.db"
runpy.run_path("planspiel.py", run_name="__main__")
