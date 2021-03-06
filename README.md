# Robotoff

Robotoff is a service managing potential Openfoodfacts updates (also known as _insights_).
These insights include a growing set of facts, including:
- the product category, weight, brand, packager codes and expiration date
- some of its labels
- abusive pictures (selfies)
- rotated pictures
- ingredient spellchecking

Robotoff provides an API to:

- import a batch of insights in JSONL format
- Fetch insights
- Annotate an insight (accept or reject) and send the update to Openfoodfacts if the insight was accepted

Once generated, the insights can be applied automatically, or after a manual validation if needs be.
A scheduler takes care of regularly marking insights for automatic annotation and for sending the update to Openfoodfacts.

The [API documentation](https://github.com/openfoodfacts/robotoff/blob/master/robotoff/doc/api.md) describes the API endpoints.

## Installation

Robotoff is made of an API web server, a scheduler, a pool of asynchronous workers and a an Elasticsearch server.
All these services are available as docker images. A `docker-compose.yml` file is used for service orchestration.

To start all services, simply run:

`$ docker-compose up -d`


## Licence

Robotoff is licenced under the AGPLv3.
