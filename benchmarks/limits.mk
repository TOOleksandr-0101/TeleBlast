# Keep the official image, mounts and runtime flags defined by the starter.
include Makefile

.PHONY: cold-build strategies measured-run
cold-build:
	@docker build --platform linux/amd64 --no-cache --tag $(IMAGE) .

strategies:
	@docker run $(RUN_FLAGS) -w /app --entrypoint python $(IMAGE) -c 'import json, selector; print(json.dumps(selector.SUPPORTED_STRATEGIES))'

.PHONY: image-manifest
image-manifest:
	@docker run $(RUN_FLAGS) -w /app --entrypoint python $(IMAGE) -c 'import hashlib,json,pathlib; print(json.dumps({p.as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path(".").rglob("*") if p.is_file()}))'

measured-run:
	@docker run $(RUN_FLAGS) --volume "$(CURDIR)/benchmarks/measure.py:/measure.py:ro" --entrypoint python $(IMAGE) /measure.py $(if $(STRATEGY),--strategy "$(STRATEGY)")

.PHONY: strategy-run
strategy-run:
	@docker run $(RUN_FLAGS) --entrypoint python $(IMAGE) /app/selector.py --strategy $(STRATEGY)
