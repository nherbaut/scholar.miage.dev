IMAGE_NAME ?= nherbaut/scpushack
GIT_TAG := $(shell git tag --points-at HEAD | head -n 1)
IMAGE_TAG := $(if $(strip $(GIT_TAG)),$(GIT_TAG),develop)
IMAGE := $(IMAGE_NAME):$(IMAGE_TAG)
IMAGE_AMD64 := $(IMAGE)-amd64
IMAGE_ARM64 := $(IMAGE)-arm64
BUILDX_BUILDER ?= scpushack-builder
BUILDX_SETUP = docker buildx inspect $(BUILDX_BUILDER) >/dev/null 2>&1 || docker buildx create --name $(BUILDX_BUILDER) --driver docker-container --use

.PHONY: build push run stop log build-amd64 build-arm64 push-amd64 push-arm64 build-push-all-arch buildx-ensure

build:
	docker build . -t $(IMAGE)

push: build
	docker push $(IMAGE)
ifneq ($(strip $(GIT_TAG)),)
	docker tag $(IMAGE) $(IMAGE_NAME):latest
	docker push $(IMAGE_NAME):latest
endif

buildx-ensure:
	$(BUILDX_SETUP)
	docker buildx use $(BUILDX_BUILDER)

build-amd64: buildx-ensure
	docker buildx build --builder $(BUILDX_BUILDER) --platform linux/amd64 -t $(IMAGE_AMD64) --load .

build-arm64: buildx-ensure
	docker buildx build --builder $(BUILDX_BUILDER) --platform linux/arm64 -t $(IMAGE_ARM64) --load .

push-amd64: buildx-ensure
ifneq ($(strip $(GIT_TAG)),)
	docker buildx build --builder $(BUILDX_BUILDER) --platform linux/amd64 -t $(IMAGE_AMD64) -t $(IMAGE_NAME):latest-amd64 --push .
else
	docker buildx build --builder $(BUILDX_BUILDER) --platform linux/amd64 -t $(IMAGE_AMD64) --push .
endif

push-arm64: buildx-ensure
ifneq ($(strip $(GIT_TAG)),)
	docker buildx build --builder $(BUILDX_BUILDER) --platform linux/arm64 -t $(IMAGE_ARM64) -t $(IMAGE_NAME):latest-arm64 --push .
else
	docker buildx build --builder $(BUILDX_BUILDER) --platform linux/arm64 -t $(IMAGE_ARM64) --push .
endif

build-push-all-arch: buildx-ensure
ifneq ($(strip $(GIT_TAG)),)
	docker buildx build --builder $(BUILDX_BUILDER) --platform linux/amd64,linux/arm64 -t $(IMAGE) -t $(IMAGE_NAME):latest --push .
else
	docker buildx build --builder $(BUILDX_BUILDER) --platform linux/amd64,linux/arm64 -t $(IMAGE) --push .
endif

run:
	docker run --rm -p 8106:8000 \
		-e SHLINK_API_KEY=${SHLINK_API_KEY} \
		-e API_KEY=${API_KEY} \
		-e PYALEX_EMAIL=nicolas.herbaut@u-bordeaux.fr \
		--name "scpushack" $(IMAGE)

stop:
	docker rm -f scpushack | true

log:
	docker logs -f scpushack
