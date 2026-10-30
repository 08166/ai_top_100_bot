FROM python:3.14-slim AS requirements-stage
WORKDIR /tmp
RUN pip install --no-cache-dir poetry==2.5.1 poetry-plugin-export==1.10.1
COPY pyproject.toml poetry.lock /tmp/
RUN poetry export -f requirements.txt --output requirements.txt
RUN poetry export -f requirements.txt --only test --output requirements-test.txt

FROM python:3.14-slim AS base-stage
WORKDIR /app
COPY --from=requirements-stage /tmp/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --no-deps -r /app/requirements.txt
RUN groupadd -r bot && useradd --no-log-init -r -g bot bot
# 볼륨 최초 생성 시 이 디렉터리 내용이 볼륨으로 복사됨 → 컨테이너 사용자가 대화 기록을 쓸 수 있게 미리 만들어 둠
RUN mkdir -p /app/data && chown bot:bot /app/data

FROM base-stage AS develop-stage
COPY --from=requirements-stage /tmp/requirements-test.txt /app/requirements-test.txt
RUN pip install --no-cache-dir --no-deps -r /app/requirements-test.txt
COPY --chown=bot:bot . /app
RUN chown bot:bot /app
USER bot
CMD ["python", "bot-test.py"]

FROM base-stage AS production-stage
COPY bot-test.py /app/
COPY prompts/ /app/prompts/
USER bot
CMD ["python", "bot-test.py"]
