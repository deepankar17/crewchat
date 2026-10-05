# For Glama's MCP catalogue (glama.ai): Glama builds this image, starts it and lists crewchat's
# tools over stdio. It is not a way to run crewchat: crewchat runs on your own machine, next to
# your project folders and agents (see the README).
FROM python:3.12-slim

COPY pyproject.toml README.md LICENSE crewchat.py crewchat_cloud.py crewchat_peers.py /opt/crewchat/
RUN pip install --no-cache-dir /opt/crewchat && useradd --create-home crew

USER crew
WORKDIR /home/crew/project
# A chat for this one folder, then MCP over stdin and stdout. What `start` prints goes to stderr,
# so that stdout carries only MCP.
CMD ["sh", "-c", "crewchat start --no-service --no-open --project crewchat --place glama --client claude >&2 && exec crewchat stdio"]
