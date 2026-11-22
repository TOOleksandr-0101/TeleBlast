include benchmarks/limits.mk

.PHONY: independent-default
independent-default:
	@docker run $(RUN_FLAGS) $(IMAGE)
