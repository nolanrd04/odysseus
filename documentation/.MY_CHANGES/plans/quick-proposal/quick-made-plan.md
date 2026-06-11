# Overall Idea
A single button to open a new UI window. This button allows the user to upload a pdf or image set as well as any additional context, then click run. The output is a proposal for the set of plans. Each step an AI takes (thinking chains, tool calls) should have their inputs and outputs displayed. On run:

## Startup:
Claude acts as a manager agent. It calls gemini for visual extractions and related tools as a sub agent it can talk to. We would essentially need to have a sub-chat happening.

## Extraction:
During extraction, gemini can call tools to pick out images and enhance certain parts to extract all necessary data. The extraction phase is iterative, claude and gemini communicate with eachother until it is determined no more possible data can be extracted

### Extaction phase 1:
Claude opens a new, **sub-chat** for gemini. This chat is visible to the user but the user cannot interact. Claude gives gemini and inital (fixed probably) prompt to go through each available page, classify it, and give bounding boxes to all available dense areas of text that could potentially have missed extraction information. Bounding boxes can overlap eachother, but everything should be boxed that isn't white space. Then, all images get displayed on the sidebar with the classification names and their bounding boxes displayed.

### Extraction phase 2:
The **chat indexing** gets created. This is essentially a temporary database that the AI's can continuously reference instead of guessing. The first index would by the pages. Then we need an index for the generated bounding boxes and the parent they relate to. The inital bounding boxes will belong to the pages but going forward the idea is that the bounding boxes can be recursive and can generate sub-boxes to extract more detail. This is the **enhance tool**. We then need to generate an index for all *attached* jobs we have in the knowledge base. There should be a UI where we can pick which jobs we want to reference. In the future, the next index we would need is for enhanced images. These are images that are generated because gemini did not succeed in extracting specific data with high confidence. When gemini returns a null value or a low confidence score, claude says to gemini, "call the enhance tool on the region with this data and re-extract." These generated images are stored here for future reference.

It is important that the AI's maintain spatial awareness, so they aren't duplicating values for overallping bounding boxes. The index aims to solve this problem. 

### Extraction phase 3: 
This is the phase where the actual data is extracted by gemini. Gemini goes through the plan(s) and extracts all of the data claude needs to aggregate. It can call tools like **web search** for getting standards or data by geographical region, **enhance** to increase a specfic part of an image's resolution, and the **index** to access already available information rather than searching for it again.

### Extraction phase 4:
All data is aggregated and a final output is generated for claude to reason with.

## Aggregation

###
This is where claude simply takes the data and generates a proposal using **web search** for external information and the **index** for existing statistics. This phase is shorter in description because claude doesn't usually struggle with this.

# Tools

### The enhance tool:
When the "enhance" tool is called, the enhanced image and its new bounding boxes need to be outputted in the chat for debug/logging.
The purpose of this? I have found that even though the content is the same, simply cropping an image to a specific region can allow the AI's to properly extract data. The enhance tool MUST call on the original image or PDF uploaded, even for recursive bounding boxes. This should be possible dynamically with linear algebra math. This is because if we extract an image from a pdf at 224dpi, then do it again at 224 dpi for a road segment, the road segment will be lower resolution than if we just extract the road segment from the pdf at 224.

### The index tool:
This is simply a temporary database where all necessary information can be repeatedly referenced, rather than just going by memory or having to search through files. Things that will be indexed:
1. The original plan set
2. Bounding boxes
3. Enhanced images
4. Information pulled from the web
5. Knowledge packs
6. Rules

### The sub-chat tool:
This is another chatbox that is created and managed by the head AI agent (claude). This is primarily used for the extraction phase. This chat needs to stay available because at the end, if the user has questions, claude can go back and ask gemini if it needs to.