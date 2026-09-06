# images/agent/Makefile
VERSION  ?= 0.2.0-nanobot
REGISTRY ?=
IMAGE    := $(REGISTRY)agent-runtime
# Build context is the monorepo root (two levels up from this Makefile).
ROOT     := ../..

.PHONY: image image-slim
image:
	docker buildx build --build-arg VARIANT=full \
	  -f Dockerfile \
	  -t $(IMAGE):$(VERSION) -t $(IMAGE):$(VERSION)-full -t $(IMAGE):latest \
	  $(ROOT)

image-slim:
	docker buildx build --build-arg VARIANT=slim \
	  -f Dockerfile \
	  -t $(IMAGE):$(VERSION)-slim \
	  $(ROOT)

# Build + push the supplied amd64 runtime to a registry.
# REGISTRY must end with a trailing slash, e.g. registry.example.com/
# Other architectures require a compatible upstream image and fresh acceptance tests.
.PHONY: image-push
image-push:
	@test -n "$(REGISTRY)" || { echo "REGISTRY required, e.g. REGISTRY=registry.example.com/"; exit 1; }
	docker buildx build --build-arg VARIANT=full \
	  --platform linux/amd64 \
	  -f Dockerfile \
	  -t $(REGISTRY)agent-runtime:$(VERSION) \
	  -t $(REGISTRY)agent-runtime:$(VERSION)-full \
	  --push \
	  $(ROOT)
