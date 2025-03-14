import json
import os

import numpy as np
import yaml

from semantic_router.encoders import (
    BaseEncoder,
    CohereEncoder,
    OpenAIEncoder,
)
from semantic_router.linear import similarity_matrix, top_scores
from semantic_router.utils.logger import logger

from semantic_router.route import Route
from semantic_router.schema import Encoder, EncoderType, RouteChoice


def is_valid(layer_config: str) -> bool:
    try:
        output_json = json.loads(layer_config)
        required_keys = ["encoder_name", "encoder_type", "routes"]

        if isinstance(output_json, list):
            for item in output_json:
                missing_keys = [key for key in required_keys if key not in item]
                if missing_keys:
                    logger.warning(
                        f"Missing keys in layer config: {', '.join(missing_keys)}"
                    )
                    return False
            return True
        else:
            missing_keys = [key for key in required_keys if key not in output_json]
            if missing_keys:
                logger.warning(
                    f"Missing keys in layer config: {', '.join(missing_keys)}"
                )
                return False
            else:
                return True
    except json.JSONDecodeError as e:
        logger.error(e)
        return False


class LayerConfig:
    """
    Generates a LayerConfig object that can be used for initializing a
    RouteLayer.
    """

    routes: list[Route] = []

    def __init__(
        self,
        routes: list[Route] = [],
        encoder_type: str = "openai",
        encoder_name: str | None = None,
    ):
        self.encoder_type = encoder_type
        if encoder_name is None:
            # if encoder_name is not provided, use the default encoder for type
            if encoder_type == EncoderType.OPENAI:
                encoder_name = "text-embedding-ada-002"
            elif encoder_type == EncoderType.COHERE:
                encoder_name = "embed-english-v3.0"
            elif encoder_type == EncoderType.HUGGINGFACE:
                raise NotImplementedError
            logger.info(f"Using default {encoder_type} encoder: {encoder_name}")
        self.encoder_name = encoder_name
        self.routes = routes

    @classmethod
    def from_file(cls, path: str):
        """Load the routes from a file in JSON or YAML format"""
        logger.info(f"Loading route config from {path}")
        _, ext = os.path.splitext(path)
        with open(path, "r") as f:
            if ext == ".json":
                layer = json.load(f)
            elif ext in [".yaml", ".yml"]:
                layer = yaml.safe_load(f)
            else:
                raise ValueError(
                    "Unsupported file type. Only .json and .yaml are supported"
                )

            route_config_str = json.dumps(layer)
            if is_valid(route_config_str):
                encoder_type = layer["encoder_type"]
                encoder_name = layer["encoder_name"]
                routes = [Route.from_dict(route) for route in layer["routes"]]
                return cls(
                    encoder_type=encoder_type, encoder_name=encoder_name, routes=routes
                )
            else:
                raise Exception("Invalid config JSON or YAML")

    def to_dict(self):
        return {
            "encoder_type": self.encoder_type,
            "encoder_name": self.encoder_name,
            "routes": [route.to_dict() for route in self.routes],
        }

    def to_file(self, path: str):
        """Save the routes to a file in JSON or YAML format"""
        logger.info(f"Saving route config to {path}")
        _, ext = os.path.splitext(path)
        with open(path, "w") as f:
            if ext == ".json":
                json.dump(self.to_dict(), f, indent=4)
            elif ext in [".yaml", ".yml"]:
                yaml.safe_dump(self.to_dict(), f)
            else:
                raise ValueError(
                    "Unsupported file type. Only .json and .yaml are supported"
                )

    def add(self, route: Route):
        self.routes.append(route)
        logger.info(f"Added route `{route.name}`")

    def get(self, name: str) -> Route | None:
        for route in self.routes:
            if route.name == name:
                return route
        logger.error(f"Route `{name}` not found")
        return None

    def remove(self, name: str):
        if name not in [route.name for route in self.routes]:
            logger.error(f"Route `{name}` not found")
        else:
            self.routes = [route for route in self.routes if route.name != name]
            logger.info(f"Removed route `{name}`")


class RouteLayer:
    index: np.ndarray | None = None
    categories: np.ndarray | None = None
    score_threshold: float = 0.82

    def __init__(
        self, encoder: BaseEncoder | None = None, routes: list[Route] | None = None
    ):
        logger.info("Initializing RouteLayer")
        self.index = None
        self.categories = None
        self.encoder = encoder if encoder is not None else CohereEncoder()
        self.routes: list[Route] = routes if routes is not None else []
        # decide on default threshold based on encoder
        if isinstance(encoder, OpenAIEncoder):
            self.score_threshold = 0.82
        elif isinstance(encoder, CohereEncoder):
            self.score_threshold = 0.3
        else:
            self.score_threshold = 0.82
        # if routes list has been passed, we initialize index now
        if len(self.routes) > 0:
            # initialize index now
            self._add_routes(routes=self.routes)

    def __call__(self, text: str) -> RouteChoice:
        results = self._query(text)
        top_class, top_class_scores = self._semantic_classify(results)
        passed = self._pass_threshold(top_class_scores, self.score_threshold)
        if passed:
            # get chosen route object
            route = [route for route in self.routes if route.name == top_class][0]
            return route(text)
        else:
            # if no route passes threshold, return empty route choice
            return RouteChoice()

    def __str__(self):
        return (
            f"RouteLayer(encoder={self.encoder}, "
            f"score_threshold={self.score_threshold}, "
            f"routes={self.routes})"
        )

    @classmethod
    def from_json(cls, file_path: str):
        config = LayerConfig.from_file(file_path)
        encoder = Encoder(type=config.encoder_type, name=config.encoder_name).model
        return cls(encoder=encoder, routes=config.routes)

    @classmethod
    def from_yaml(cls, file_path: str):
        config = LayerConfig.from_file(file_path)
        encoder = Encoder(type=config.encoder_type, name=config.encoder_name).model
        return cls(encoder=encoder, routes=config.routes)

    @classmethod
    def from_config(cls, config: LayerConfig):
        encoder = Encoder(type=config.encoder_type, name=config.encoder_name).model
        return cls(encoder=encoder, routes=config.routes)

    def add(self, route: Route):
        print(f"Adding route `{route.name}`")
        # create embeddings
        embeds = self.encoder(route.utterances)

        # create route array
        if self.categories is None:
            print("Initializing categories array")
            self.categories = np.array([route.name] * len(embeds))
        else:
            print("Adding route to categories")
            str_arr = np.array([route.name] * len(embeds))
            self.categories = np.concatenate([self.categories, str_arr])
        # create utterance array (the index)
        if self.index is None:
            print("Initializing index array")
            self.index = np.array(embeds)
        else:
            print("Adding route to index")
            embed_arr = np.array(embeds)
            self.index = np.concatenate([self.index, embed_arr])
        # add route to routes list
        self.routes.append(route)

    def _add_routes(self, routes: list[Route]):
        # create embeddings for all routes
        all_utterances = [
            utterance for route in routes for utterance in route.utterances
        ]
        embedded_utterance = self.encoder(all_utterances)

        # create route array
        route_names = [route.name for route in routes for _ in route.utterances]
        route_array = np.array(route_names)
        self.categories = (
            np.concatenate([self.categories, route_array])
            if self.categories is not None
            else route_array
        )

        # create utterance array (the index)
        embed_utterance_arr = np.array(embedded_utterance)
        self.index = (
            np.concatenate([self.index, embed_utterance_arr])
            if self.index is not None
            else embed_utterance_arr
        )

    def _query(self, text: str, top_k: int = 5):
        """Given some text, encodes and searches the index vector space to
        retrieve the top_k most similar records.
        """
        # create query vector
        xq = np.array(self.encoder([text]))
        xq = np.squeeze(xq)  # Reduce to 1d array.

        if self.index is not None:
            # calculate similarity matrix
            sim = similarity_matrix(xq, self.index)
            scores, idx = top_scores(sim, top_k)
            # get the utterance categories (route names)
            routes = self.categories[idx] if self.categories is not None else []
            return [{"route": d, "score": s.item()} for d, s in zip(routes, scores)]
        else:
            logger.warning("No index found for route layer.")
            return []

    def _semantic_classify(self, query_results: list[dict]) -> tuple[str, list[float]]:
        scores_by_class: dict[str, list[float]] = {}
        for result in query_results:
            score = result["score"]
            route = result["route"]
            if route in scores_by_class:
                scores_by_class[route].append(score)
            else:
                scores_by_class[route] = [score]

        # Calculate total score for each class
        total_scores = {route: sum(scores) for route, scores in scores_by_class.items()}
        top_class = max(total_scores, key=lambda x: total_scores[x], default=None)

        # Return the top class and its associated scores
        if top_class is not None:
            return str(top_class), scores_by_class.get(top_class, [])
        else:
            logger.warning("No classification found for semantic classifier.")
            return "", []

    def _pass_threshold(self, scores: list[float], threshold: float) -> bool:
        if scores:
            return max(scores) > threshold
        else:
            return False

    def to_config(self) -> LayerConfig:
        return LayerConfig(
            encoder_type=self.encoder.type,
            encoder_name=self.encoder.name,
            routes=self.routes,
        )

    def to_json(self, file_path: str):
        config = self.to_config()
        config.to_file(file_path)

    def to_yaml(self, file_path: str):
        config = self.to_config()
        config.to_file(file_path)
