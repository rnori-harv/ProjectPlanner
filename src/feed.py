import PyPDF2
from dotenv import dotenv_values, load_dotenv, find_dotenv
import openai
import langchain
from langchain import SerpAPIWrapper
import pypdf
import time
import re
from typing import List, Union



from langchain.document_loaders import PyPDFLoader
from langchain.indexes import VectorstoreIndexCreator
from langchain.chains import RetrievalQA
from langchain.llms import OpenAI
from langchain.text_splitter import CharacterTextSplitter
from langchain.embeddings import OpenAIEmbeddings
from langchain.vectorstores import Chroma
from langchain.prompts import PromptTemplate, BaseChatPromptTemplate
from langchain.chains import LLMChain
from langchain.agents import load_tools, Tool, AgentOutputParser
from langchain.agents import initialize_agent
from langchain.schema import HumanMessage

from langchain.agents import Tool, AgentExecutor, LLMSingleActionAgent, AgentOutputParser
from langchain.prompts import BaseChatPromptTemplate, ChatPromptTemplate
from langchain import SerpAPIWrapper, LLMChain
from langchain.schema import AgentAction, AgentFinish, HumanMessage, SystemMessage
# LLM wrapper
from langchain.chat_models import ChatOpenAI
from langchain import OpenAI
# Conversational memory
from langchain.memory import ConversationBufferWindowMemory
# Embeddings and vectorstore
from langchain.embeddings.openai import OpenAIEmbeddings
from langchain.vectorstores import Pinecone

import streamlit as st  

env_vars = dotenv_values('../.env')
load_dotenv(dotenv_path='../.env')

# Access the values
openai.api_key = env_vars['OPENAI_API_KEY']
OPENAI_API_KEY = env_vars['OPENAI_API_KEY']

st.title('NotesWise')
st.write('NotesWise is a tool that allows you to ask questions about your notes and get answers from your notes. It is powered by OpenAI\'s GPT-4 and LangChain\'s LangLearner Model. To get started, upload your notes in PDF format below.')

# BASIC MODEL with Prompt engineering
def read_pdf(file_path):
    with open(file_path, 'rb') as file:
        reader = PyPDF2.PdfReader(file)
        num_pages = len(reader.pages)
        ans = ""

        for page_number in range(num_pages):
            page = reader.pages[page_number]
            text = page.extract_text()
            ans += text
        return ans

def pass_knowledge_to_openai(text):
    prompt = "You have been given information" 
    response = openai.Completion.create(
        engine='text-davinci-003',
        prompt=prompt,
        max_tokens=100,
        temperature=0.7,
        n=1,
        stop=None
    )
    generated_text = response.choices[0].text.strip()
    return generated_text


def load_langchain_model(file_paths):
    documents = [PyPDFLoader(file_path).load_and_split()[0] for file_path in file_paths]

    # select which embeddings we want to use
    embeddings = OpenAIEmbeddings()
    # create the vectorestore to use as the index
    db = Chroma.from_documents(documents, embeddings)
    # expose this index in a retriever interface
    retriever = db.as_retriever(search_type="similarity", search_kwargs={"k":2})
    # create a chain to answer questions 
    qa = RetrievalQA.from_chain_type(
        llm=OpenAI(), chain_type="stuff", retriever=retriever, return_source_documents=True)
    return qa

def query_langchain_model(model, query):
    ans = model({"query": query})
    print(ans)
    return ans["result"], ans["source_documents"]

search = SerpAPIWrapper()

# Set up a prompt template
class CustomPromptTemplate(BaseChatPromptTemplate):
    # The template to use
    template: str
    # The list of tools available
    tools: List[Tool]
    
    def format_messages(self, **kwargs) -> str:
        # Get the intermediate steps (AgentAction, Observation tuples)
        
        # Format them in a particular way
        intermediate_steps = kwargs.pop("intermediate_steps")
        thoughts = ""
        for action, observation in intermediate_steps:
            thoughts += action.log
            thoughts += f"\nObservation: {observation}\nThought: "
            
        # Set the agent_scratchpad variable to that value
        kwargs["agent_scratchpad"] = thoughts
        
        # Create a tools variable from the list of tools provided
        kwargs["tools"] = "\n".join([f"{tool.name}: {tool.description}" for tool in self.tools])
        
        # Create a list of tool names for the tools provided
        kwargs["tool_names"] = ", ".join([tool.name for tool in self.tools])
        formatted = self.template.format(**kwargs)
        return [HumanMessage(content=formatted)]

class CustomOutputParser(AgentOutputParser):
    
    def parse(self, llm_output: str) -> Union[AgentAction, AgentFinish]:
        
        # Check if agent should finish
        if "Final Answer:" in llm_output:
            return AgentFinish(
                # Return values is generally always a dictionary with a single `output` key
                # It is not recommended to try anything else at the moment :)
                return_values={"output": llm_output.split("Final Answer:")[-1].strip()},
                log=llm_output,
            )
        
        # Parse out the action and action input
        regex = r"Action: (.*?)[\n]*Action Input:[\s]*(.*)"
        match = re.search(regex, llm_output, re.DOTALL)
        
        # If it can't parse the output it raises an error
        # You can add your own logic here to handle errors in a different way i.e. pass to a human, give a canned response
        if not match:
            raise ValueError(f"Could not parse LLM output: `{llm_output}`")
        action = match.group(1).strip()
        action_input = match.group(2)
        
        # Return the action and action input
        return AgentAction(tool=action, tool_input=action_input.strip(" ").strip('"'), log=llm_output)
    


def llm_agent():
    llm = OpenAI(temperature=0.1)
    tools = [
                Tool(name = "Source Info", func = get_source_info, description = "Useful for when you need to consult information within your knowledge base. Use this before the other tool search.")
        Tool(name = "Search", func = search.run, description = "Useful for when you need to consult information outside of your knowledge base.")
    ]

    # agent = initialize_agent(tools, llm, verbose=True)

    my_template = """Answer the following questions as best you can, but speaking as a tutor would speak. You have access to the following tools:
                {tools}

                Use the following format:

                Question: the input question you must answer
                Thought: you should always think about what to do
                Action: the action to take, should be one of [{tool_names}]
                Action Input: the input to the action
                Observation: the result of the action
                ... (this Thought/Action/Action Input/Observation can repeat N times)
                Thought: I now know the final answer
                Final Answer: the final answer to the original input question

                Begin! Remember to speak as a teaching assistant when giving your final answer.

                Question: {input}
                {agent_scratchpad}"""

    prompt = CustomPromptTemplate(
                template=my_template,
                tools=tools,
                # This omits the `agent_scratchpad`, `tools`, and `tool_names` variables because those are generated dynamically
                # This includes the `intermediate_steps` variable because that is needed
                input_variables=["input", "intermediate_steps"],
            )
    llm_chain = LLMChain(llm=llm, prompt=prompt)

    # Using tools, the LLM chain and output_parser to make an agent
    tool_names = [tool.name for tool in tools]
    output_parser = CustomOutputParser()

    agent = LLMSingleActionAgent(
        llm_chain=llm_chain, 
        output_parser=output_parser,
        # We use "Observation" as our stop sequence so it will stop when it receives Tool output
        # If you change your prompt template you'll need to adjust this as well
        stop=["\nObservation:"], 
        allowed_tools=tool_names
    )
    agent_executor = AgentExecutor.from_agent_and_tools(agent=agent, tools=tools, verbose=True)
    return agent_executor

    
pdf_file_paths = ['./152/lec01-intro.pdf', './152/lec02-smallstep.pdf', './152/lec03-inductive-proof.pdf'
                  , './152/lec04-largestep.pdf', './152/lec05-imp.pdf', './152/lec06-denotational.pdf']

# BASIC MODEL with Prompt engineering
#pased_text = read_pdf(pdf_file_path)
#print(pass_knowledge_to_openai(pased_text))

# LANGCHAIN MODEL:
files = st.file_uploader("Upload your lecture note files (PDF)", type=["pdf"], accept_multiple_files=True)
while files == []:
    time.sleep(0.5)
file_paths = []
for file in files:
    file_paths.append(file.name)


def get_source_info(prompt):
    res, source_docs = query_langchain_model(model, prompt)
    information_consulted = []
    for doc in source_docs:
        information_consulted.append(doc.page_content)
    return information_consulted




model = load_langchain_model(file_paths)

# User input
prompt = st.text_input('Enter your question here:')
if prompt != '':
    res, source_docs = query_langchain_model(model, prompt)
    st.write(res)
    st.write("Source information consulted:")
    information_consulted = []
    for doc in source_docs:
        information_consulted.append(doc.page_content)
        st.write(doc.metadata["source"] + ", Page " + str(doc.metadata["page"] + 1))

    my_agent = llm_agent()
    ans = my_agent.run(prompt)
    st.write(ans)

    

