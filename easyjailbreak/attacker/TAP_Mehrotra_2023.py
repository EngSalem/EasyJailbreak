r"""
'Tree of Attacks' Recipe
============================================
This module implements a jailbreak method describe in the paper below.
This part of code is based on the code from the paper.

Paper title: Tree of Attacks: Jailbreaking Black-Box LLMs Automatically
arXiv link: https://arxiv.org/abs/2312.02119
Source repository: https://github.com/RICommunity/TAP
"""
import os
import logging
from tqdm import tqdm

from easyjailbreak.attacker import AttackerBase
from easyjailbreak.datasets import JailbreakDataset
from easyjailbreak.datasets.instance import Instance
from easyjailbreak.loggers.logger import Logger
from easyjailbreak.models.huggingface_model import HuggingfaceModel
from easyjailbreak.models.openai_model import OpenaiModel
####### 4 major components #######
from easyjailbreak.seed.seed_template import SeedTemplate
from easyjailbreak.mutation.generation.IntrospectGeneration import IntrospectGeneration
from easyjailbreak.constraint.DeleteOffTopic import DeleteOffTopic
from easyjailbreak.metrics.Evaluator.Evaluator_GenerativeGetScore import EvaluatorGenerativeGetScore
from easyjailbreak.selector.SelectBasedOnScores import SelectBasedOnScores

r"""
EasyJailbreak TAP class
============================================
"""
__all__ = ['TAP']

target_model_calls = 0

class TAP(AttackerBase):
    r"""
    Tree of Attack method, an extension of PAIR method. Use 4 phases:
    1. Branching
    2. Pruning: (phase 1)
    3. Query and Access
    4. Pruning: (phase 2)

    >>> from easyjailbreak.attacker.TAP_Mehrotra_2023 import TAP
    >>> from easyjailbreak.models.huggingface_model import from_pretrained
    >>> from easyjailbreak.datasets.jailbreak_datasets import JailbreakDataset
    >>> from easyjailbreak.datasets.Instance import Instance
    >>> attack_model = from_pretrained(model_path_1)
    >>> target_model = from_pretrained(model_path_2)
    >>> eval_model  = from_pretrained(model_path_3)
    >>> dataset = JailbreakDataset('AdvBench')
    >>> attacker = TAP(attack_model, target_model, eval_model, dataset)
    >>> attacker.attack()
    >>> attacker.jailbreak_Dataset.save_to_jsonl("./TAP_results.jsonl")
    """
    def __init__(self, attack_model, target_model, eval_model, jailbreak_datasets: JailbreakDataset,
                 tree_width=10, tree_depth=10,root_num=1, branching_factor=4,keep_last_n=3,
                 max_n_attack_attempts=5, template_file=None,
                 attack_max_n_tokens=500,
                 attack_temperature=1,
                 attack_top_p=0.9,
                 target_max_n_tokens=150,
                 target_temperature=1,
                 target_top_p=1,
                 judge_max_n_tokens=10,
                 judge_temperature=1):
        """
        initialize TAP, inherit from AttackerBase

        :param  ~HuggingfaceModel|~OpenaiModel attack_model: LLM for generating jailbreak prompts during Branching(mutation)
        :param  ~HuggingfaceModel|~OpenaiModel target_model: LLM being attacked to generate adversarial responses
        :param  ~HuggingfaceModel|~OpenaiModel eval_model: LLM for evaluating during Pruning:phase1(constraint) and Pruning:phase2(select)
        :param  ~JailbreakDataset jailbreak_datasets: containing instances which conveys the query and reference responses
        :param  int tree_width: defining the max width of the conversation nodes during Branching(mutation)
        :param  int tree_depth: defining the max iteration of a single instance
        :param  int root_num: defining the number of trees or batch of a single instance
        :param  int branching_factor: defining the number of children nodes generated by a parent node during Branching(mutation)
        :param  int keep_last_n: defining the number of rounds of dialogue to keep during Branching(mutation)
        :param  int max_n_attack_attempts: defining the max number of attempts to generating a valid adversarial prompt of a branch
        :param  str template_file: file path of the seed_template.json
        :param  int attack_max_n_tokens: max_n_tokens of the target model
        :param  float attack_temperature: temperature of the attack model
        :param  float attack_top_p: top p of the attack_model
        :param  int target_max_n_tokens: max_n_tokens of the target model
        :param  float target_temperature: temperature of the target model
        :param  float target_top_p: top_p of the target model
        :param  int judge_max_n_tokens: max_n_tokens of the target model
        :param  float judge_temperature: temperature of the judge model
        """
        super().__init__(attack_model=attack_model,
                         target_model=target_model,
                         eval_model=eval_model,
                         jailbreak_datasets=jailbreak_datasets)
        self.seeds=SeedTemplate().new_seeds(1,method_list=['TAP'],template_file=template_file)

        ####### 4 major components ##########
        self.mutator=IntrospectGeneration(attack_model,
                                          system_prompt=self.seeds[0],
                                          keep_last_n=keep_last_n,
                                          branching_factor=branching_factor,
                                          max_n_attack_attempts=max_n_attack_attempts)
        self.constraint=DeleteOffTopic(self.eval_model, tree_width)
        self.selector=SelectBasedOnScores(jailbreak_datasets, tree_width)
        self.evaluator=EvaluatorGenerativeGetScore(self.eval_model)

        ######## logging information ############
        self.current_query: int = 0
        self.current_jailbreak: int = 0
        self.current_reject: int = 0
        self.current_iteration: int = 0

        ######## parameters of TAP tree #########
        self.root_num = root_num
        self.tree_depth = tree_depth
        self.tree_width = tree_width
        self.branching_factor = branching_factor

        ######## datasets and logger ############
        self.jailbreak_Dataset = JailbreakDataset([])
        self.logger = Logger()

        ######## model configuration ############
        self.target_max_n_tokens = target_max_n_tokens
        self.target_temperature = target_temperature
        self.target_top_p = target_top_p
        self.judge_temperature = judge_temperature
        self.judge_max_n_tokens = judge_max_n_tokens

        if self.attack_model.generation_config == {}:
            if isinstance(self.attack_model, OpenaiModel):
                self.attack_model.generation_config = {'max_tokens': attack_max_n_tokens,
                                                       'temperature': attack_temperature,
                                                       'top_p': attack_top_p}
            elif isinstance(self.attack_model, HuggingfaceModel):
                self.attack_model.generation_config = {'max_new_tokens': attack_max_n_tokens,
                                                       'temperature': attack_temperature,
                                                       'do_sample': True,
                                                       'top_p': attack_top_p,
                                                       'eos_token_id': self.attack_model.tokenizer.eos_token_id}

        if isinstance(self.eval_model, OpenaiModel) and self.eval_model.generation_config == {}:
            self.eval_model.generation_config = {'max_tokens': self.judge_max_n_tokens,
                                                 'temperature': self.judge_temperature}
        elif isinstance(self.eval_model, HuggingfaceModel) and self.eval_model.generation_config == {}:
            self.eval_model.generation_config = {'do_sample': True,
                                                 'max_new_tokens': self.judge_max_n_tokens,
                                                 'temperature': self.judge_temperature}

    def attack(self, save_path='TAP_attack_result.jsonl'):
        r"""
        Execute the attack process using provided prompts.
        """
        # To calculate how many times are eval_model.generate() called
        global target_model_calls
        logging.info("Jailbreak started!")
        try:
            for Instance in tqdm(self.jailbreak_datasets, desc="Processing instances"):
                new_Instance = self.single_attack(Instance)[0]
                self.jailbreak_Dataset.add(new_Instance)
        except KeyboardInterrupt:
            logging.info("Jailbreak interrupted by user!")
        self.update(self.jailbreak_Dataset)
        print(f'jailbreak_prompt:{[instance.jailbreak_prompt for instance in self.jailbreak_Dataset]}')
        print(f'target_responses:{[instance.target_responses[0] for instance in self.jailbreak_Dataset]}')
        print(f"ASR:{100*self.current_jailbreak/self.current_query}%")
        print(f"Total calls of generate:{target_model_calls}")
        print(f"Eval calls of generate:{self.evaluator.eval_model.generate.count_calls - target_model_calls}")
        self.log()
        logging.info("Jailbreak finished!")
        self.jailbreak_Dataset.save_to_jsonl(save_path)
        logging.info(
            'Jailbreak result saved at {}!'.format(os.path.join(os.path.dirname(os.path.abspath(__file__)), save_path))
        )

    def single_attack(self, instance) -> JailbreakDataset:
        r"""
        Conduct an attack for an instance.

        :param ~Instance instance: The Instance that is attacked.
        :return ~JailbreakDataset: returns the attack result dataset.
        """
        global target_model_calls
        batch=[JailbreakDataset([instance.copy()]) for _ in range(self.root_num)]
        find_flag = 0
        print(f"QUERY:{'='*20}\n{instance.query}")
        for iteration in range(1, self.tree_depth + 1):
            print(f"""\n{'=' * 36}\nTree-depth is: {iteration}\n{'=' * 36}\n""", flush=True)
            dataset_list = []
            for i,stream in enumerate(batch):
                print(f"BATCH:{i}")
                new_dataset = stream

                ############# generate jailbreak_prompts by branching ################
                new_dataset = self.mutator(new_dataset)

                ############# prune off-topic jailbreak_prompt ################
                new_dataset = self.constraint(new_dataset)

                ############# attack ################
                self.target_model.conversation.messages = []
                for instance in new_dataset:
                    if isinstance(self.target_model, OpenaiModel):
                        instance.target_responses = [
                            self.target_model.generate(instance.jailbreak_prompt, max_tokens=self.target_max_n_tokens,
                                                    temperature=self.target_temperature, top_p=self.target_top_p)]
                    elif isinstance(self.target_model, HuggingfaceModel):
                        instance.target_responses = [
                            self.target_model.generate(instance.jailbreak_prompt,
                                                    max_new_tokens=self.target_max_n_tokens,
                                                    temperature=self.target_temperature, do_sample=True,
                                                    top_p=self.target_top_p,
                                                    eos_token_id=self.target_model.tokenizer.eos_token_id)]
                    target_model_calls+=1

                ############# prune not-jailbroken jailbreak_prompt ################
                num_responses = len(new_dataset)
                self.evaluator(new_dataset)
                new_dataset = self.selector.select(new_dataset)
                # print(f"""\n\t{'=' * 36}\n\tCount of Calls of Evaluator is: {self.evaluator.eval_model.generate.calls - num_responses}\n{'=' * 36}\n""", flush=True)

                batch[i] = new_dataset
                ############# attack successful ################
                if any([instance.eval_results[-1] == 10 for instance in new_dataset]):
                    find_flag = 1
                    print("Found a jailbreak. Exiting.")
                    break
            if find_flag:
                new_instance = max(new_dataset, key=lambda instance: instance.eval_results[-1])
                new_instance.eval_results=[1]
                break
        if iteration == self.tree_depth:
            new_instance = max(new_dataset, key=lambda instance: instance.eval_results[-1])
            new_instance.eval_results=[0]
        return JailbreakDataset([new_instance])

    def update(self, Dataset: JailbreakDataset):
        r"""
        Update the state of the ReNeLLM based on the evaluation results of Datasets.

        :param ~JailbreakDataset: processed dataset after an iteration
        """
        for prompt_node in Dataset:
            self.current_jailbreak += prompt_node.num_jailbreak
            self.current_query += prompt_node.num_query
            self.current_reject += prompt_node.num_reject

    def log(self):
        r"""
        Report the attack results.
        """
        logging.info("======Jailbreak report:======")
        logging.info(f"Total queries: {self.current_query}")
        logging.info(f"Total jailbreak: {self.current_jailbreak}")
        logging.info(f"Total reject: {self.current_reject}")
        logging.info("========Report End===========")
