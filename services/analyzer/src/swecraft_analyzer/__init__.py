"""Post-hoc analysis of one society checkpoint.

``collect`` turns a checkpoint directory into one SQLite evidence store;
``detect`` runs mechanical detectors over that store and writes candidate
findings with evidence pointers. Neither step uses a model: judging the
candidates is left to the analyst agents that read the outputs.
"""

__version__ = "0.2.0"
