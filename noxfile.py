import re
import time

import nox

# mkdocstrings fetches the Ansible objects.inv inventory at build time for
# cross-reference links, and docs.ansible.com (fronted by Read the Docs /
# Cloudflare) rate-limits the shared IP pool GitHub Actions runners use.
# That rate limiting has been observed to persist for many minutes at a
# time, well beyond what's reasonable to cover with retries alone. Retry a
# few times in case it's a short blip, then fall back to treating a build
# that failed *only* because of this known, external issue as a pass --
# any other warning still fails the build as strict mode intends.
MKDOCS_BUILD_ATTEMPTS = 3
MKDOCS_RETRY_BASE_DELAY_SECONDS = 30
INVENTORY_RATE_LIMIT_RE = re.compile(
    r"Couldn't load inventory .*docs\.ansible\.com.*HTTP Error 429"
)


@nox.session
def build(session: nox.Session):
    """
    Build the AWX Operator docsite.
    """
    session.install(
        "-r",
        "docs/requirements.in",
        "-c",
        "docs/requirements.txt",
    )

    for attempt in range(1, MKDOCS_BUILD_ATTEMPTS + 1):
        output = session.run(
            "mkdocs",
            "build",
            "--strict",
            *session.posargs,
            silent=True,
            success_codes=range(256),
        )
        print(output)

        if "Aborted with" not in output:
            return

        offending_lines = [
            line for line in output.splitlines() if line.startswith(("WARNING", "ERROR"))
        ]
        if offending_lines and all(INVENTORY_RATE_LIMIT_RE.search(line) for line in offending_lines):
            session.log(
                "mkdocs build otherwise succeeded; the only failure was the "
                "Ansible docs inventory download being rate-limited "
                "(HTTP 429 from docs.ansible.com), a known external issue "
                "unrelated to this repo's docs -- treating as non-fatal."
            )
            return

        if attempt == MKDOCS_BUILD_ATTEMPTS:
            session.error("mkdocs build failed")

        delay = MKDOCS_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
        session.log(
            f"mkdocs build failed (attempt {attempt}/{MKDOCS_BUILD_ATTEMPTS}); "
            f"retrying in {delay}s"
        )
        time.sleep(delay)
