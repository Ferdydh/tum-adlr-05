from src.core.config import DataConfig, MLPExperimentConfig
from src.models import pl_se3
from src.core.train import train

if __name__ == "__main__":
    config: MLPExperimentConfig = MLPExperimentConfig.default()
    config.data = DataConfig.sanity()
    config.trainer.max_epochs = 100

    # Initialize model
    model = pl_se3.FlowMatching(config)

    train(model, config)
