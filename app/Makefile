IMAGE ?= nherbaut/scpushack:nextnet

.PHONY: build push run stop log

build:
	docker build . -t $(IMAGE)

push: build
	docker push $(IMAGE)

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
