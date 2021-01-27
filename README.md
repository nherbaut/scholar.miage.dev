# Install

## Install docker and docker-compose if necessary:

```bash
# docker-ce
$ wget https://get.docker.com -O get.docker.sh sh ./get.docker.sh

# To run docker without sudo (remember to log out and back in for this to take effect!)
$ sudo groupadd docker && sudo usermod -aG docker $USER

# docker compose
$ sudo curl -L "https://github.com/docker/compose/releases/download/1.24.0/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
$ sudo chmod +x /usr/local/bin/docker-compose
```

## Build all the services and create the database:

```bash
$ docker-compose up

# Create the DB
$ docker-compose exec web bash -c 'PGPASSWORD=$POSTGRES_PASSWORD createdb -U $POSTGRES_USER -h db $POSTGRES_USER'
```

## Seed the database:

```python
# /!\ Execute these commands inside the web container
$ python
>>> from app import db
>>> db.create_all(); exit()
# Set alembic revision to latest to avoid running all the previous migrations
$ flask db stamp head
```

## Start the project:

```
docker-compose up
```

---

# Code generation

We rely on [OpenAPIs code generation](http://openapis.org/) and sql reverse engineering to create back-end code for rest APIs and persistence.

**/!\ All these commands must be executed inside the web container /!\\**

## Python rest api

Generates the python-flask REST API layer from its [openapi specification](http://spec.openapis.org/oas/v3.0.2) in file api-definition.yml.

```bash
make build-api
```

## Python persistence models

Generates the persistence model objects from a live SQL connection specified in the Makefile.

```bash
make build-model
```

## Javascript client API

Generates the typescript-jquery client for the API.

```bash
make build-api-client
```

## Ruby client API

Generates the ruby client for the API.

```bash
make build-api-client-ruby
```

---

## Production

it should run as a WSGI app, behind https
