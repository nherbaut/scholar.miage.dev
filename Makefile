build:
	docker buildx build --platform linux/amd64,linux/arm64 . -t  nherbaut/scpushack:nextnet --push
	#docker buildx build --platform linux/amd64 . -t  nherbaut/scpushack:nextnet  --load
push:
	docker push nherbaut/scpushack:nextnet
run:
	docker run --rm -p 8106:5000 -e API_KEY=5976d0e4859f487086eb4ad43d3b851a -e ORCID_CLIENT_ID="APP-I35HRS942VHSVKW4" -e ORCID_CLIENT_SECRET="844ae7d9-e7fa-4430-9398-6f30c91b7398" --name "scpushack" nherbaut/scpushack:nextnet
stop:
	docker rm -f scpushack | true
log:
	docker logs -f scpushack



