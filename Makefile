build:
	docker build . -t nherbaut/scpushack
push:
	docker push nherbaut/scpushack
run:
	docker run -d -p 8106:5000 -e SHLINK_API_KEY=${SHLINK_API_KEY} -e API_KEY=${API_KEY}  --name "scpushack" nherbaut/scpushack
stop:
	docker rm -f scpushack | true
log:
	docker logs -f scpushack



