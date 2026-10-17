FROM mcr.microsoft.com/playwright/python@sha256:80fd7c1aad9600ea348572dd46ca00b9ea31d890831f5838fc61319ab79900d2
LABEL onefoundry.browser.base_digest="sha256:80fd7c1aad9600ea348572dd46ca00b9ea31d890831f5838fc61319ab79900d2"

# The base contains browser binaries but deliberately omits the Python package.
RUN python -m pip install --no-cache-dir playwright==1.61.0
COPY browser_fixture_child.py /opt/onefoundry/browser_fixture_child.py
ENV PYTHONDONTWRITEBYTECODE=1
