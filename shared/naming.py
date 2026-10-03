"""Mandatory resource-naming policy for every resource created in this repository.

See REQUIREMENTS.md ("Naming policy") for the human-readable version.

Physical resource names (bucket names, function names, cluster names, ...)
use `-` as the separator. `_` is used only where the AWS resource type's
own naming rules forbid hyphens (for example, some identifiers require
starting with a letter and allow only `[a-zA-Z0-9_]`) - `resource_name()`
takes `separator="_"` for those cases; the call site is expected to know
which its resource type needs (see each service's own naming-rules page,
linked from the module's README.md).

The "Name" *tag* is handled separately, in `shared/tagging.py` - this module
only builds the *physical name* string passed to the resource's own naming
parameter (`bucket_name=`, `function_name=`, `cluster_name=`, ...).
"""

from __future__ import annotations

import hashlib
import re


def resource_name(*parts: str, separator: str = "-") -> str:
    """Build a resource name from `parts`, lower-cased and joined by `separator`.

    Example: resource_name("acme", "dev", "orders", "queue") -> "acme-dev-orders-queue"
    """
    if separator not in ("-", "_"):
        raise ValueError('separator must be "-" or "_"')

    cleaned = [str(part).strip().lower() for part in parts if str(part).strip()]
    name = separator.join(cleaned)

    allowed_chars = r"a-z0-9\-" if separator == "-" else r"a-z0-9_"
    name = re.sub(rf"[^{allowed_chars}]", separator, name)
    name = re.sub(rf"{re.escape(separator)}{{2,}}", separator, name)
    return name.strip(separator)


def bounded_name(*parts: str, max_length: int, separator: str = "-") -> str:
    """`resource_name(*parts)`, shortened to `max_length` characters if needed.

    Some names have tight limits - Elastic Load Balancing load balancer and
    target group names, for example, allow at most 32 characters (see
    https://docs.aws.amazon.com/elasticloadbalancing/latest/APIReference/API_CreateTargetGroup.html).
    A longer name keeps its beginning and gets a 6-character hash of the
    full name appended, so it stays unique and is the same on every synth.

    Example: bounded_name("learning-ecs", "test", "tg", "nlb-internal", max_length=32)
             -> "learning-ecs-test-tg-nl-<hash6>"
    """
    name = resource_name(*parts, separator=separator)
    if len(name) <= max_length:
        return name
    digest = hashlib.sha1(name.encode()).hexdigest()[:6]
    head = name[: max_length - len(digest) - 1].rstrip(separator)
    return f"{head}{separator}{digest}"
