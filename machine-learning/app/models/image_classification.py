import sys
from io import BytesIO
from pathlib import Path
from typing import Any
from huggingface_hub import snapshot_download
from optimum.onnxruntime import ORTModelForImageClassification
from optimum.pipelines import pipeline as optimum_pipeline
from PIL import Image
from transformers import AutoImageProcessor
from openvino.runtime import Core
from ..config import log
from ..schemas import ModelType
from .base import InferenceModel
try:
    from optimum.intel.openvino import OVModelForImageClassification
    from transformers import pipeline as transformers_pipeline
except ImportError:
    log.warn("the image is not built with openvino, hardware acceleration using openvino will be disabled")


class ImageClassifier(InferenceModel):
    _model_type = ModelType.IMAGE_CLASSIFICATION

    def __init__(
        self,
        model_name: str,
        min_score: float = 0.9,
        cache_dir: Path | str | None = None,
        **model_kwargs: Any,
    ) -> None:
        self.min_score = model_kwargs.pop("minScore", min_score)
        super().__init__(model_name, cache_dir, **model_kwargs)

    def _download(self) -> None:
        snapshot_download(
            cache_dir=self.cache_dir,
            repo_id=self.model_name,
            allow_patterns=["*.bin", "*.json", "*.txt"],
            local_dir=self.cache_dir,
            local_dir_use_symlinks=True,
        )

    def _load(self) -> None:
        processor = AutoImageProcessor.from_pretrained(self.cache_dir, cache_dir=self.cache_dir)
        model_kwargs = {
            "cache_dir": self.cache_dir,
            "provider": self.providers[0],
            "provider_options": self.provider_options[0],
        }
        # detect if Openvino is built with and compatible hardware exists in the environment
        if "optimum.intel.openvino" in sys.modules and "GPU" in Core().available_devices:
            model_path = self.cache_dir/ "ov_model"
            if model_path.exists():
                model = OVModelForImageClassification.from_pretrained(model_path,compile=False, **model_kwargs)
            else:
                log.info(
                    (
                        f"Openvino model not found in cache directory for '{self.model_name}'."
                        "Exporting optimized model for future use."
                    ),
                )
                model = OVModelForImageClassification.from_pretrained(self.model_name,  compile=False,**model_kwargs,export=True)
                model.save_pretrained(model_path)

            model.to("gpu")
            model.reshape(batch_size=1, sequence_length=3, height=224, width=224)
            model.compile()
            self.model = transformers_pipeline(self.model_type.value, model, feature_extractor=processor)

        else:

            model_path = self.cache_dir / "model.onnx"
            if model_path.exists():
                model = ORTModelForImageClassification.from_pretrained(self.cache_dir, **model_kwargs)
                self.model = optimum_pipeline(self.model_type.value, model, feature_extractor=processor)
            else:
                log.info(
                    (
                        f"ONNX model not found in cache directory for '{self.model_name}'."
                        "Exporting optimized model for future use."
                    ),
                )
                self.sess_options.optimized_model_filepath = model_path.as_posix()
                self.model = optimum_pipeline(
                    self.model_type.value,
                    self.model_name,
                    model_kwargs=model_kwargs,
                    feature_extractor=processor,
                )

    def _predict(self, image: Image.Image | bytes) -> list[str]:
        if isinstance(image, bytes):
            image = Image.open(BytesIO(image))
        predictions: list[dict[str, Any]] = self.model(image)
        tags = [tag for pred in predictions for tag in pred["label"].split(", ") if pred["score"] >= self.min_score]
        return tags

    def configure(self, **model_kwargs: Any) -> None:
        self.min_score = model_kwargs.pop("minScore", self.min_score)
