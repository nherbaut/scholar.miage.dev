arm64:
	docker buildx build --platform linux/arm64 . -t  nherbaut/scpushack:nextnet --push
amd64:
	docker buildx build --platform linux/amd64 . -t  nherbaut/scpushack:nextnet --push

build: arm64 amd64

push:
	docker push nherbaut/scpushack:nextnet
run:
	docker run --rm -p 8106:8000  -e SHLINK_API_KEY=${SHLINK_API_KEY} -e API_KEY=${API_KEY} -e PYALEX_EMAIL=nicolas.herbaut@u-bordeaux.fr --name "scpushack" nherbaut/scpushack:nextnet
stop:
	docker rm -f scpushack | true
log:
	docker logs -f scpushack



