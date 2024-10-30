import os
import json
import re
import datetime
import textwrap
from openai import OpenAI, OpenAIError
from dotenv import load_dotenv
import pymssql
from colorama import init, Fore, Style
from TicketInfo import TicketAggregator
from MSGraphAuthenticate import Authenticate, TeamsSearch

# Initialize colorama
init(autoreset=True)

# Load environment variables
load_dotenv()

# OpenAI client
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
client = OpenAI(api_key=OPENAI_API_KEY)

# SQL server connection details
GP_SERVER = os.getenv('GP_SERVER')
GP_DATABASE = os.getenv('GP_DATABASE')
GRT_USER = os.getenv('GRT_USER')
GRT_PASS = os.getenv('GRT_PASS')

# Base SQL query template
base_gp_query = """SELECT DISTINCT
    COALESCE(sop10100.SOPNUMBE, sop30300.SOPNUMBE) AS 'Equipment Ticket',
    COALESCE(sop10100.CSTPONBR, sop30200.CSTPONBR) AS 'Account Number',
    CASE
        WHEN (COALESCE(sop10100.BACHNUMB, sop30200.BACHNUMB) IN ('RDY TO INVOICE', 'RDY TO INV') OR COALESCE(sop10100.BACHNUMB, sop30200.BACHNUMB) LIKE 'Q%')
            THEN 'RDY TO INVOICE'
        ELSE COALESCE(sop10100.BACHNUMB, sop30200.BACHNUMB)
    END AS 'Queue',
    COALESCE(sop10100.CUSTNAME, sop30200.CUSTNAME) AS 'Customer Name',
    spv3SalesDocument.xProject_Name AS 'Project Name',
    COALESCE(sop10200.ITEMNMBR, sop30300.ITEMNMBR) AS 'Item Number',
    COALESCE(sop10200.ITEMDESC, sop30300.ITEMDESC) AS 'Item Description',
    STR(
        CASE
            WHEN FLOOR(COALESCE(sop10200.QUANTITY, sop30300.QUANTITY)) = COALESCE(sop10200.QUANTITY, sop30300.QUANTITY)
                THEN CAST(COALESCE(sop10200.QUANTITY, sop30300.QUANTITY) AS INT)
            ELSE COALESCE(sop10200.QUANTITY, sop30300.QUANTITY)
        END
    ) AS 'Quantity',
    sop10201.SERLTNUM AS 'Serial Number',
    CAST(spv3SalesDocument.Notes AS NVARCHAR(MAX)) AS 'Internal Notes',
    COALESCE(sop10100.ReqShipDate, sop30300.ReqShipDate) AS 'Requested Ship Date',
    COALESCE(sop10100.CITY, sop30300.CITY) AS 'City',
    COALESCE(sop10100.STATE, sop30300.STATE) AS 'State',
    sop10107.Tracking_Number
FROM sop10100
FULL OUTER JOIN sop30300 ON sop10100.SOPNUMBE = sop30300.SOPNUMBE
LEFT JOIN sop10200 ON COALESCE(sop10100.SOPNUMBE, sop30300.SOPNUMBE) = sop10200.SOPNUMBE
LEFT JOIN sop30200 ON sop30300.SOPNUMBE = sop30200.SOPNUMBE
LEFT JOIN sop10201 ON sop10201.ITEMNMBR = COALESCE(sop10200.ITEMNMBR, sop30300.ITEMNMBR)
                    AND sop10201.SOPNUMBE = COALESCE(sop10100.SOPNUMBE, sop30300.SOPNUMBE)
LEFT JOIN sop10107 ON sop10107.SOPNUMBE = COALESCE(sop10100.SOPNUMBE, sop30300.SOPNUMBE)
LEFT JOIN spv3SalesDocument ON spv3SalesDocument.Sales_Doc_Num = COALESCE(sop10100.SOPNUMBE, sop30300.SOPNUMBE)
WHERE COALESCE(SOP10100.SOPTYPE, SOP30300.SOPTYPE) in (1, 2)
"""

# Regex patterns
ticket_pattern = re.compile(r'\b(?:CW)?(?:[1-9]\d{5,8})(?:[.-]\d+)?\b', re.IGNORECASE)
account_pattern = re.compile(r'\b\d{7,8}\b')

# Initialize conversation history and contextual ticket numbers
conversation_history = []
last_ticket_number = None  # Track last ticket number for follow-up reference


def print_token_usage(response):
    """
    Prints the number of tokens used in the API response, if available.
    """
    # Ensure the response has 'usage' and that it’s a CompletionUsage object
    if hasattr(response, 'usage') and response.usage:
        total_tokens = response.usage.total_tokens
        prompt_tokens = response.usage.prompt_tokens
        completion_tokens = response.usage.completion_tokens
        print(f"Token usage - Total: {total_tokens}, Prompt: {prompt_tokens}, Completion: {completion_tokens}")
    else:
        print("Token usage data not found in the response.")


def determine_context(prompt):
    """
    Determines the context of the user prompt: 'chat', 'ticket', or 'database_search'.
    """
    # Gather recent conversation history (last 6 messages)
    recent_history = ""
    for entry in conversation_history[-6:]:
        recent_history += f"{entry['role']}: {entry['content']}\n"

    context_prompt = f"""
You are an internal assistant for Granite Telecommunications, helping with ticket inquiries and database searches.

Here is the recent conversation history:
{recent_history}

Determine the context of the following user input:
"{prompt}"

Respond with one of the following options (without quotes):
- chat: If the user is engaging in general conversation or small talk.
- ticket: If the user is asking about a specific ticket number or requesting details about a known ticket. This includes follow-up questions regarding a ticket from the recent conversation history.
- database_search: If the user is asking for information from the database, such as searching for tickets based on serial numbers, account numbers, item numbers, customer names, or any other criteria.

Important Notes:
- If the user is asking "what ticket" something is on/under/associated with, use database_search.
- Ticket numbers are 6 to 9-digit numbers, possibly prefixed with 'CW', and never start with '0'.
- Account numbers are numeric-only strings with exactly 7 or 8 digits and may or may not have a leading zero.
- Serial numbers are alphanumeric strings that may contain letters and numbers in any sequence.
- Queries involving searching for tickets using serial numbers, item numbers, customer names, account numbers, or other details should be classified as 'database_search'.
- If the user is asking to find a ticket based on any piece of information other than a ticket number, classify it as 'database_search'.

Only respond with one of the following words without any quotation marks: chat, ticket, or database_search.
Do not include any quotes or additional text in your response.
"""
    try:
        print("Determining context.")
        chat_completion = client.chat.completions.create(
            messages=[
                {"role": "system", "content": context_prompt},
            ],
            model="gpt-4o-mini",
        )
        # print_token_usage(chat_completion)

        gpt_response = chat_completion.choices[0].message.content.strip().lower()
        gpt_response = gpt_response.strip("'\"").strip()
        return gpt_response

    except OpenAIError as e:
        print(f"Error determining context: {str(e)}")
        return "chat"  # Default to general chat in case of an error


def normalize_ticket_number(ticket_num):
    ticket_str = str(ticket_num).strip()
    ticket_str = re.sub(r'^CW', '', ticket_str, flags=re.IGNORECASE)
    ticket_str = re.sub(r'[.-]\d+$', '', ticket_str)
    return ticket_str


def get_recent_ticket_number():
    """
    Return the most recent ticket number based on last_ticket_number for direct reference.
    """
    global last_ticket_number
    return last_ticket_number


def generate_sql_query(prompt):
    """
    Generates an SQL query based on the user's prompt and the base SQL query using GPT.
    """
    schema_info = """
The database contains the following fields for each ticket:
- Equipment Ticket
- Account Number
- Queue
- Customer Name
- Project Name
- Item Number
- Item Description
- Quantity
- Serial Number
- Requested Ship Date
- City
- State
- Tracking Number

Notes:
- Serial numbers and item numbers are alphanumeric strings.
- Serial numbers are device identifiers and not personal data.
- Account numbers are numeric-only strings with exactly 7 or 8 digits and may or may not have a leading zero (e.g., '03807975' or '3807975').
- When querying by account number, use a LIKE clause with a wildcard to match accounts with or without a leading zero (e.g., LIKE '%{account_number}').
- Tickets may be in different queues, and you may need to filter based on the queue status. "RDY TO INVOICE" indicates a closed ticket.
"""

    query_prompt = f"""
You are an assistant that generates SQL queries to help users retrieve information from the database.

Based on the base SQL query provided below, modify it to retrieve data that satisfies the following user request:

User request: "{prompt}"

Base SQL query:
{base_gp_query}

{schema_info}

Important Instructions:
- Incorporate the user's request by adding appropriate WHERE clauses or modifying the query as needed.
- If the user's request includes a serial number, ensure to filter by the 'Serial Number' field.
- Ensure all referenced tables are properly joined.
- Do not include any columns from unjoined tables.
- Use DISTINCT where necessary.
- Provide only the modified SQL query enclosed within ```sql and ``` delimiters.
- Do not include any explanations or additional text outside the SQL code.
"""

    try:
        print("Generating SQL query.")
        chat_completion = client.chat.completions.create(
            messages=[
                {"role": "system",
                 "content": "You are an assistant that generates SQL queries to help users retrieve information from the database."},
                {"role": "user", "content": query_prompt}
            ],
            model="gpt-4o-mini",
        )
#         print_token_usage(chat_completion)
        response_content = chat_completion.choices[0].message.content.strip()
        # print(f"GPT Response:\n{response_content}")  # Add this line to debug
        sql_code_match = re.search(r'```sql\n(.*?)\n```', response_content, re.DOTALL | re.IGNORECASE)
        if sql_code_match:
            sql_query = sql_code_match.group(1).strip()
        else:
            sql_query = response_content
        return sql_query

    except OpenAIError as e:
        print(f"Error generating SQL query: {str(e)}")
        return None


def execute_query(sql_query):
    """
    Executes the provided SQL query against the configured SQL Server and returns the results.
    """
    try:
        print("Executing SQL query.")

        if not sql_query.strip().upper().startswith('SELECT'):
            return "Invalid SQL query: The query does not start with 'SELECT'."

        # Check if the SQL query seems incomplete
        if sql_query.strip().endswith(','):
            return "Invalid SQL query: The query appears to be incomplete."

        with pymssql.connect(GP_SERVER, GRT_USER, GRT_PASS, GP_DATABASE, tds_version="7.0") as conn:
            with conn.cursor(as_dict=True) as cursor:
                if "TOP" not in sql_query.upper():
                    sql_query = sql_query.replace("SELECT DISTINCT", "SELECT DISTINCT TOP 100", 1)
                cursor.execute(sql_query)
                results = cursor.fetchall()

                if not results:
                    return "No data found."

                for row in results:
                    for key, value in row.items():
                        if isinstance(value, datetime.datetime):
                            row[key] = value.strftime('%Y-%m-%d %H:%M:%S')
                        elif isinstance(value, datetime.date):
                            row[key] = value.strftime('%Y-%m-%d')

                return results

    except pymssql.DatabaseError as e:
        return f"SQL execution error: {str(e)}"
    except Exception as e:
        return f"Unexpected error during SQL execution: {str(e)}"


def summarize_chat_data(chat_data):
    """
    Summarizes the chat data using GPT.
    """
    try:
        data_json = json.dumps(chat_data, indent=4)
        prompt = f"""
You are a helpful assistant. Summarize the following Microsoft Teams chat data related to a ticket:

{data_json}

Provide a concise summary highlighting the key discussions and any important messages. Use bullet points where appropriate. Do not include any unnecessary information.
"""
        chat_completion = client.chat.completions.create(
            messages=[
                {"role": "system", "content": "You are a helpful assistant that summarizes chat data."},
                {"role": "user", "content": prompt},
            ],
            model="gpt-4o-mini",
        )
#         print_token_usage(chat_completion)
        summary = chat_completion.choices[0].message.content.strip()
        return summary
    except OpenAIError as e:
        return f"Error summarizing chat data: {str(e)}"
    except Exception as e:
        return f"Unexpected error in summarize_chat_data: {str(e)}"


def get_ticket_info(ticket_num, user_prompt):
    """
    Retrieves detailed information for a specific ticket, including data from MS Teams, and returns a response to the user's prompt.
    """
    try:
        print(f"Fetching ticket information for ticket number: {ticket_num}")
        aggregator = TicketAggregator(ticket_num)
        ticket_data = aggregator.aggregate_data()  # Retrieve the aggregated data
        # print(f"Ticket Data Retrieved: {ticket_data}")

        # Retrieve MS Teams chat data
        auth_instance = Authenticate()
        teams_search = TeamsSearch(auth_instance)
        chat_data = teams_search.get_conversations(search_term=ticket_num)
        # print(f"Chat Data Retrieved: {chat_data}")

        # Prepare the data for the final prompt, clearly separating chat data
        data = {
            "ticket_data": str(ticket_data),
            "ms_teams_chat_data": str(chat_data)  # Explicitly labeled for clarity
        }

        # Send both data sets to respond_to_prompt_with_data
        response = respond_to_prompt_with_data(user_prompt, data)
        return response

    except Exception as e:
        print(f"Exception in get_ticket_info: {e}")
        return "There was an error fetching the ticket information. Please try again later."


def respond_to_prompt_with_data(prompt, data):
    """
    Provides a response to the user's prompt using the data provided, focusing on MS Teams chat data if requested.
    """
    try:
        if not data:
            return "No data available to provide an answer."

        # Prepare ticket data JSON and full MS Teams chat
        ticket_data_json = json.dumps(data.get('ticket_data', {}), indent=2)
        ms_teams_chat_data = json.dumps(data.get('ms_teams_chat_data', {}), indent=2)

        # Generate the response with full MS Teams chat data included
        final_prompt = f"""
        You are a helpful assistant.

        Here is the ticket data:
        {ticket_data_json}

        Here is the complete Microsoft Teams chat data:
        {ms_teams_chat_data}

        All of the above Microsoft Teams chat data is relevant to the user request. Use it directly in your response as needed.
        You do not need to specify that it is from Microsoft Teams chat, we all know that's where it's coming from.

        Based on the above information, please respond to the user's request:
        "{prompt}"

        Instructions:
        - Respond in plain text only, without any Markdown, bullet points, lists, or special formatting.
        - Use the data provided to answer the user's request.
        - Answer directly and concisely in complete sentences, in a clear and understandable manner.
        """

        chat_completion = client.chat.completions.create(
            messages=[
                {"role": "system",
                 "content": "You are a helpful assistant that uses the provided data to answer questions."},
                {"role": "user", "content": final_prompt},
            ],
            model="gpt-4o-mini",
        )
#         print_token_usage(chat_completion)
        assistant_response = chat_completion.choices[0].message.content.strip()
        return assistant_response

    except OpenAIError as e:
        return f"Error generating response: {str(e)}"
    except Exception as e:
        return f"Unexpected error: {str(e)}"


def generate_chat_response(prompt):
    """
    Generates a general chat response using GPT.
    """
    try:
        print("Generating chat response.")

        # Include the most recent conversation history (limit to last 6 messages)
        history_text = ''
        for entry in conversation_history[-6:]:
            role = entry['role']
            content = entry['content']
            history_text += f"{role}: {content}\n"

        chat_prompt = f"""
You are an internal assistant at a telecommunications company, communicating with a colleague.
Engage with the user in a conversational manner, focusing on providing helpful and professional responses.
The user is a coworker, not a customer.

Conversation History:
{history_text}

User: "{prompt}"

Instructions:
- Use the conversation history to inform your response.
- Respond in plain text without any markdown or formatting symbols.
- Provide responses in paragraph form unless the user specifically requests a list.
- Keep the tone professional and collegial.
- Avoid overenthusiastic language.
- Do not refer to "our services" or treat the user as a customer.
- Use language appropriate for internal communication between colleagues.
- Use full sentences unless the user requests otherwise.
"""
        chat_completion = client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": "You are an internal assistant at Granite Telecommunications, communicating with a colleague.",
                },
                {"role": "user", "content": chat_prompt},
            ],
            model="gpt-4o-mini",
        )
#         print_token_usage(chat_completion)

        assistant_response = chat_completion.choices[0].message.content.strip()
        return assistant_response

    except OpenAIError as e:
        return f"Error generating chat response: {str(e)}"
    except Exception as e:
        return f"Unexpected error in generate_chat_response: {str(e)}"


def process_user_prompt(prompt):
    global last_ticket_number

    # Determine context with GPT
    intent = determine_context(prompt)
    conversation_history.append({"role": "user", "content": prompt})

    print(f"Detected Intent: {intent}")

    # Extract ticket number from user prompt
    match = ticket_pattern.search(prompt)
    if match:
        last_ticket_number = normalize_ticket_number(match.group())

    if intent == 'ticket':
        if last_ticket_number:
            # Fetch and return detailed ticket information
            ticket_info = get_ticket_info(last_ticket_number, prompt)
            conversation_history.append({"role": "assistant", "content": ticket_info})

            # Extract any ticket numbers from the bot response and update `last_ticket_number`
            response_ticket_match = ticket_pattern.search(ticket_info)
            if response_ticket_match:
                last_ticket_number = normalize_ticket_number(response_ticket_match.group())

            return '\n'.join(textwrap.wrap(ticket_info, width=100))
        else:
            # Ask user to specify a ticket number if none was found
            bot_response = "Could you please specify the ticket number?"
            conversation_history.append({"role": "assistant", "content": bot_response})
            return bot_response

    if intent == 'database_search':
        account_match = account_pattern.search(prompt)
        if account_match:
            account_num = account_match.group()
            sql_query = generate_sql_query(f"Find open tickets under account number {account_num}")
        else:
            sql_query = generate_sql_query(prompt)

        if not sql_query:
            bot_response = "I'm sorry, I couldn't generate a query based on your request."
            conversation_history.append({"role": "assistant", "content": bot_response})
            return bot_response

        query_results = execute_query(sql_query)
        if isinstance(query_results, str):
            response = query_results
        else:
            data = {
                "ticket_data": query_results
            }
            response = respond_to_prompt_with_data(prompt, data)

            # Update `last_ticket_number` with the first valid ticket found in query results
            for row in query_results:
                if 'Equipment Ticket' in row and validate_ticket_number(row['Equipment Ticket']):
                    last_ticket_number = normalize_ticket_number(row['Equipment Ticket'])
                    break

        # Extract any ticket numbers from the bot response and update `last_ticket_number`
        response_ticket_match = ticket_pattern.search(response)
        if response_ticket_match:
            last_ticket_number = normalize_ticket_number(response_ticket_match.group())

        conversation_history.append({"role": "assistant", "content": response})
        return '\n'.join(textwrap.wrap(response, width=100))

    elif intent == 'chat':
        chat_response = generate_chat_response(prompt)
        conversation_history.append({"role": "assistant", "content": chat_response})

        # Check for ticket numbers in the chat response, just in case
        response_ticket_match = ticket_pattern.search(chat_response)
        if response_ticket_match:
            last_ticket_number = normalize_ticket_number(response_ticket_match.group())

        return '\n'.join(textwrap.wrap(chat_response, width=100))

    else:
        # Default response if intent is unclear
        bot_response = "I'm not sure how to assist with that request. Could you please provide more details?"
        conversation_history.append({"role": "assistant", "content": bot_response})
        return bot_response


def validate_ticket_number(ticket_num):
    """
    Checks if the provided ticket number matches the expected ticket format.
    Returns True if valid, False otherwise.
    """
    return bool(ticket_pattern.fullmatch(ticket_num))


def main():
    global conversation_history, last_ticket_number
    conversation_history = []
    last_ticket_number = None
    while True:
        try:
            user_prompt = input(Style.BRIGHT + Fore.LIGHTRED_EX + 'UserPrompt: ' + Style.RESET_ALL)
            if user_prompt.lower() in ["exit", "quit"]:
                print("Exiting GraniteBot as per user request.")
                break
            result = process_user_prompt(user_prompt)
            print(
                Style.BRIGHT + Fore.LIGHTBLUE_EX + 'GraniteBot: ' + Style.RESET_ALL + Fore.LIGHTCYAN_EX + result + Style.RESET_ALL)

        except Exception as e:
            print(f"An unexpected error occurred: {e}")


if __name__ == '__main__':
    main()
