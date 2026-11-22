# Keep the official image, mounts and runtime flags defined by the starter.
include Makefile

.PHONY: measured-run
measured-run:
	@docker run $(RUN_FLAGS) --volume "$(CURDIR)/benchmarks/measure.py:/measure.py:ro" --entrypoint python $(IMAGE) /measure.py
