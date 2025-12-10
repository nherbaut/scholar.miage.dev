IMAGE_NAME ?= nherbaut/scpushack
GIT_TAG := $(shell git tag --points-at HEAD | head -n 1)
IMAGE_TAG := $(if $(strip $(GIT_TAG)),$(GIT_TAG),develop)
IMAGE := $(IMAGE_NAME):$(IMAGE_TAG)

.PHONY: build push run stop log

build:
	docker build . -t $(IMAGE)

push: build
	docker push $(IMAGE)
ifneq ($(strip $(GIT_TAG)),)
	docker tag $(IMAGE) $(IMAGE_NAME):latest
	docker push $(IMAGE_NAME):latest
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
