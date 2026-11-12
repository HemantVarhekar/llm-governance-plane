"""Pluggable PII classifiers.

Every classifier satisfies the `Classifier` protocol in policy.py — two
methods, no inheritance — so the gateway swaps one for another by changing a
single line of config.yaml. That is the plug-and-play property applied to
the compliance layer, not just to models.

    regex      structured identifiers only. Fast, zero deps, CANNOT do names.
    presidio   spaCy NER. Catches PERSON and LOCATION. Separate process.
    composite  runs both and UNIONS their findings.
"""

from .composite import CompositeClassifier
from .presidio_client import PresidioClassifier

__all__ = ["CompositeClassifier", "PresidioClassifier"]
