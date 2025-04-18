/* Main implementation file for handling chat */

const cds = require('@sap/cds');
const { DELETE } = cds.ql;
const { storeRetrieveMessages, storeModelResponse } = require('./memory-helper');


const tableName = 'SAP_TISCE_DEMO_DOCUMENTCHUNK';
const embeddingColumn = 'EMBEDDING';
const contentColumn = 'TEXT_CHUNK';

const systemPrompt =
  ` You are Granite Chat, an AI language model developed by IBM. You are a cautious assistant. You carefully follow instructions. You are helpful and harmless and you follow ethical guidelines and promote positive behavior. You always respond to greetings (for example, hi, hello, g'day, morning, afternoon, evening, night, what's up, nice to meet you, sup) with "Hello! I am Granite Chat, created by IBM. How can I help you today?". Please do not say anything else and do not start a conversation.\n
    You are a helpful assistant who answers user question based only on the following context enclosed in triple quotes.\n
  `;

async function getOrchestrationChatResponse(query, chatModelName, chatModelConfig, similarContent, history) {

    const capllmplugin = await cds.connect.to("cap-llm-plugin");
    const clientConfig = {
        llm: {
          model_name: chatModelName,
          model_params: chatModelConfig
        },
        templating: {
          template: [
          { role: 'system', content: ` ${systemPrompt} \`\`\` ${similarContent} \`\`\` ` },
          { role: 'user', content:  '{{?query}}?'}
          ]
        }
      };

    const chatCompletionConfig = {
        inputParams: { query },
        messagesHistory : history
      };


    const chatResponse = await capllmplugin.getHarmonizedChatCompletion({
        clientConfig,
        chatCompletionConfig,
        getContent: true // Retrieve only the content
    });
    
    return chatResponse;
  }


module.exports = function () {

    this.on('getChatRagResponse', async (req) => {
        try {
            //request input data
            const { conversationId, messageId, message_time, user_id, user_query } = req.data;
            const { Conversation, Message } = this.entities;
            const capllmplugin = await cds.connect.to("cap-llm-plugin");
            console.log("***********************************************************************************************\n");
            console.log(`Received the request for RAG retrieval for the user query : ${user_query}\n`);

            //Obtain the model configs configured in package.json
            const chatModelName = cds.env.requires["gen-ai-hub"]["chat-model"]["modelName"];
            const chatModelConfig = cds.env.requires["gen-ai-hub"]["chat-model"]["model_params"];
            const embeddingModelName = cds.env.requires["gen-ai-hub"]["embedding-model"]["modelName"];
            const embeddingModelConfig = cds.env.requires["gen-ai-hub"]["embedding-model"];

            console.log(`Leveraing the following LLMs \n Chat Model: ${chatModelName} \n Embedding Model: ${embeddingModelName}\n`);
            console.log(`Model config\n\n`)
            console.log(chatModelConfig)
            //generate embeddings
            console.log(`\nGenerating the vector embeddings for the user query: ${user_query}\n\n`);
            const embeddingResult = await capllmplugin.getEmbeddingWithConfig(embeddingModelConfig, user_query);
            const embedding = embeddingResult?.data[0]?.embedding;
            console.log("***********************************************************************************************\n");
            
            //get the top relevant content from similarity search
            console.log("Retreiving the top relevant content from similarity search from SAP HANA Cloud Vector Engine.\n\n");
            const similaritySearchResults = await capllmplugin.similaritySearch(
                tableName, 
                embeddingColumn, 
                contentColumn, 
                embedding, 
                "COSINE_SIMILARITY", 
                5);
            
            const similarContent = similaritySearchResults.map(
                (obj) => obj.PAGE_CONTENT
                );
            console.log("Similarity search content retrieved for the user query:\n");

            similarContent.forEach((content, index) => {
                console.log(`Result ${index + 1}:\n`);
                console.log(content);
                console.log("\n" + "-".repeat(80) + "\n"); // Adds a separator line for clarity
              });
            console.log("***********************************************************************************************\n");    

            //Optional. handle memory before the RAG LLM call
            const memoryContext = await storeRetrieveMessages(conversationId, messageId, message_time, user_id, user_query, Conversation, Message, chatModelName);  

            //chat completion response from Orchestration Service
            console.log(`Getting the chat completion response using the ${chatModelName} model via the CAP LLM Plugin.\n\n`);
            const orchestrationResponse = await getOrchestrationChatResponse(
              user_query,
              chatModelName,
              chatModelConfig,
              similarContent,
              memoryContext
            );  

            console.log(`Response received from the ${chatModelName} model: \n`, JSON.stringify(orchestrationResponse, null, 2),"\n");
            console.log("***********************************************************************************************\n");
            //parse the response object   according to the respective model for your use case.
            const chatCompletionResponse = {
              "role": "assistant",
              "content": orchestrationResponse
            }
            //Optional. handle memory after the RAG LLM call
            const responseTimestamp = new Date().toISOString();
            await storeModelResponse(conversationId, responseTimestamp, chatCompletionResponse, Message, Conversation);

            //build the response payload for the frontend.
            const response = {
                "role": chatCompletionResponse.role,
                "content": chatCompletionResponse.content,
                "messageTime": responseTimestamp,
                "additionalContents": null,
            };

            return response;  
        }
        catch (error) {
            // Handle any errors that occur during the execution
            console.log('Error while generating response for user query:', error);
            throw error;
        }
    })


    this.on('deleteChatData', async () => {
        try {
            const { Conversation, Message } = this.entities;
            await DELETE.from(Conversation);
            await DELETE.from(Message);
            return "Success!"
        }
        catch (error) {
            // Handle any errors that occur during the execution
            console.log('Error while deleting the chat content in db:', error);
            throw error;
        }
    })

}