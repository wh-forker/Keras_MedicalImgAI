import json
import os
import pickle
import shutil

from keras.callbacks import ModelCheckpoint, TensorBoard, ReduceLROnPlateau
from keras.optimizers import Adam
from keras.utils import multi_gpu_model

from app.callback import MultipleClassAUROC, MultiGPUModelCheckpoint, SaveBaseModel
from app.datasets import dataset_loader as dsload
from app.main.Actions import Actions
from app.models.model_factory import get_model


class Trainer(Actions):
    # Runtime stuffs
    history = None
    auroc = None
    model = None
    model_train = None
    checkpoint = None
    output_weights_path = None
    train_generator = None
    dev_generator = None
    training_stats = {"run": 0, "best_mean_auroc": 0, "lr": 0.001}

    def __init__(self, config_file: str):
        super().__init__(config_file)
        self.fitter_kwargs = {"verbose": int(self.conf.progress_train_verbosity), "max_queue_size": 32, "workers": 32,
                              "epochs": self.conf.epochs, "use_multiprocessing": True}

        os.makedirs(self.conf.output_dir, exist_ok=True)  # check output_dir, create it if not exists
        self.check_training_lock()

    def check_training_lock(self):
        if os.path.isfile(self.conf.train_lock_file):
            raise RuntimeError(f"A process is running in this directory {self.conf.train_lock_file}")
        else:
            open(self.conf.train_lock_file, "a").close()

    def dump_history(self):
        # dump history
        print("** dump history **")
        with open(os.path.join(self.conf.output_dir, "history.pkl"), "wb") as f:
            pickle.dump({"history": self.history.history, "auroc": self.auroc.aurocs, }, f)
        self.dump_stats()

    def dump_stats(self):
        with open(self.conf.train_stats_file, 'w') as f:
            json.dump(self.training_stats, f)

    def check_gpu_availability(self):
        self.model_train = self.model
        self.checkpoint = ModelCheckpoint(self.output_weights_path)
        print("** check multiple gpu availability **")
        gpus = len(os.getenv("CUDA_VISIBLE_DEVICES", "1").split(","))
        if gpus > 1:
            print(f"** multi_gpu_model is used! gpus={gpus} **")
            self.model_train = multi_gpu_model(self.model, gpus)
            self.model_train.base_model = self.model.base_model
            # FIXME: currently (Keras 2.1.2) checkpoint doesn't work with multi_gpu_model
            self.checkpoint = MultiGPUModelCheckpoint(
                filepath=self.output_weights_path,
                base_model=self.model,
            )

    def prepare_datasets(self):
        if self.MDConfig.is_resume_mode:
            if not os.path.isfile(self.conf.train_stats_file):
                print(f"** Resume mode is assumed but train stats {self.conf.train_stats_file} is not found")
            else:
                self.training_stats = json.load(open(self.conf.train_stats_file))
                self.conf.initial_learning_rate = self.training_stats["lr"]
                self.training_stats["run"] += 1

            print("** Run #{} - learning rate is set to ".format(
                self.training_stats["run"]) + f"** {self.conf.initial_learning_rate} **")
        else:
            print("** Run #{} - trained model weights not found, starting over **".format(self.training_stats["run"]))
        self.dump_stats()
        print(f"backup config file to {self.conf.output_dir}")
        shutil.copy(self.config_file, os.path.join(self.conf.output_dir, os.path.split(self.config_file)[1]))

        data_set = dsload.DataSet(self.conf.DatasetConfig)

        print("** create image generators **")
        self.train_generator = data_set.train_generator(verbosity=self.conf.verbosity)
        self.dev_generator = data_set.dev_generator(verbosity=self.conf.verbosity)

        if self.conf.train_steps != "auto":
            print(f"** overriding train_steps: {self.conf.train_steps} **")
            self.fitter_kwargs["steps_per_epoch"] = self.conf.train_steps

        if self.conf.validation_steps != "auto":
            print(f"** overriding validation_steps: {self.conf.validation_steps} **")
            self.fitter_kwargs["validation_steps"] = self.conf.validation_steps

        print("** compute class weights from training data **")
        self.fitter_kwargs["class_weight"] = data_set.class_weights()

        self.fitter_kwargs["generator"] = self.train_generator
        self.fitter_kwargs["validation_data"] = self.dev_generator

    def prepare_model(self):
        print(f"** Base Model = {self.MDConfig.base_model_weights_file} **")
        print(f"** Trained Model = {self.MDConfig.trained_model_weights} **")
        self.model = get_model(self.DSConfig.class_names, self.MDConfig.base_model_weights_file,
                               self.MDConfig.trained_model_weights,
                               image_dimension=self.IMConfig.img_dim, color_mode=self.IMConfig.color_mode,
                               class_mode=self.DSConfig.class_mode)
        if self.MDConfig.show_model_summary:
            print(self.model.summary())

        self.output_weights_path = os.path.join(self.conf.output_dir, self.MDConfig.output_weights_name)
        print(f"** set output weights path to: {self.output_weights_path} **")
        self.check_gpu_availability()

        print("** compile model with class weights **")
        optimizer = Adam(lr=self.conf.initial_learning_rate)
        self.model_train.compile(optimizer=optimizer, loss="binary_crossentropy")
        self.auroc = MultipleClassAUROC(generator=self.dev_generator, steps=self.conf.validation_steps,
                                        class_names=self.DSConfig.class_names, weights_path=self.output_weights_path,
                                        config=self.conf, class_mode=self.DSConfig.class_mode)

    def train(self):

        try:
            self.prepare_datasets()
            self.prepare_model()
            trained_base_weight = os.path.join(self.conf.output_dir, "trained_base_model_weight.h5")

            self.fitter_kwargs["callbacks"] = []
            self.fitter_kwargs["callbacks"].append(self.checkpoint)
            self.fitter_kwargs["callbacks"].append(TensorBoard(
                log_dir=os.path.join(self.conf.output_dir, "logs", "run{}".format(self.training_stats["run"])),
                batch_size=self.conf.batch_size, histogram_freq=0, write_graph=False,
                write_grads=False, write_images=False, embeddings_freq=0))
            self.fitter_kwargs["callbacks"].append(ReduceLROnPlateau(monitor='val_loss', factor=0.1,
                                                                     patience=self.conf.patience_reduce_lr, verbose=1))
            self.fitter_kwargs["callbacks"].append(self.auroc)
            self.fitter_kwargs["callbacks"].append(SaveBaseModel(filepath=trained_base_weight, save_weights_only=False))

            print("** training start with parameters: **")
            for k, v in self.fitter_kwargs.items():
                print(f"\t{k}: {v}")
            self.history = self.model_train.fit_generator(**self.fitter_kwargs)
            self.dump_history()

        finally:
            os.remove(self.conf.train_lock_file)
