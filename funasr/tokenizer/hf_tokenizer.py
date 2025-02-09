
try:
	from transformers import AutoTokenizer
except:
	print("If you want to use hugging, please `pip install -U transformers`")

from funasr.register import tables

@tables.register("tokenizer_classes", "HuggingfaceTokenizer")
def HuggingfaceTokenizer(init_param_path, **kwargs):

	tokenizer = AutoTokenizer.from_pretrained(init_param_path)
	
	return tokenizer

