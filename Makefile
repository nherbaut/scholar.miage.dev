build:
	docker build . -t nherbaut/scpushack
push:
	docker push nherbaut/scpushack
run:
	docker run -d -p 8106:80 -e API_KEY=${API_KEY} --name "scpushack" nherbaut/scpushack
	sleep 2
	echo Go to localhost:8106 to visit the website
stop:
	docker rm -f scpushack



