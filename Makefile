.PHONY: test verify
export IMAGE

test:
	timeout -k 10s 300s python3 -m unittest discover -s tests -p 'test_*.py'

verify: test
	@set -e; \
	if [ -z "$$IMAGE" ]; then \
		build=$$(timeout -k 30s 3600s bin/ax-orbi build --engine docker --json) || { \
			status=$$?; printf '%s\n' "$$build" >&2; exit "$$status"; \
		}; \
		IMAGE=$$(printf '%s\n' "$$build" | python3 -c 'import json, sys; print(json.load(sys.stdin)["tag"])'); \
	fi; \
	timeout -k 30s 900s verify/container.sh "$$IMAGE"
