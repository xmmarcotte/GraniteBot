import smartsheet
from smartsheet import exceptions
import pymssql
import dotenv
import requests
import os
import json
import datetime
import time
import random
import logging
import re
import decimal
from typing import Dict, Any, List

# Configure logging
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

dotenv.load_dotenv()


def normalize_ticket_number(ticket_num):
    ticket_str = str(ticket_num).strip()

    # Remove any spaces
    ticket_str = ticket_str.replace(" ", "")

    # Remove 'CW' prefix if present, with or without spaces
    ticket_str = re.sub(r'^(CW\s*-*\s*)', '', ticket_str, flags=re.IGNORECASE)

    # Remove '.X' or '-X' where X is any digit
    ticket_str = re.sub(r'(\.\d+|-\d+)$', '', ticket_str)

    return ticket_str


def exponential_backoff(attempt, max_attempts=5, base_delay=60, max_delay=300):
    if attempt >= max_attempts:
        return False
    delay = min(max_delay, base_delay * 2 ** attempt) + random.uniform(0, 10)
    print(f"Waiting for {delay:.2f} seconds before retrying...")
    time.sleep(delay)
    return True


def smartsheet_api_call_with_retry(call, *args, **kwargs):
    attempt = 0
    max_attempts = 5
    while attempt < max_attempts:
        try:
            # Attempt to call the Smartsheet SDK function
            return call(*args, **kwargs)
        except smartsheet.exceptions.ApiError as e:
            # Check if the error is due to rate limiting
            if e.error.result.error_code == 4003:
                print("Encountered rate limit error, applying exponential backoff.")
                if not exponential_backoff(attempt, max_attempts):
                    print("Max retry attempts reached for rate limit error. Giving up.")
                    return None
            elif 500 <= e.error.result.status_code < 600:
                print(f"Encountered server error with status code: {e.error.result.status_code}, applying short delay.")
                # Apply a shorter delay for 5XX errors before retrying
                if not exponential_backoff(attempt, max_attempts, base_delay=10, max_delay=60):
                    print("Max retry attempts reached for server error. Giving up.")
                    return None
            else:
                print(f"Smartsheet API error: {e}")
                return None
        except Exception as e:
            print(f"Unexpected error: {e}")
            return None
        attempt += 1


class GetSSInfo:
    def __init__(self, ticket_id):
        self.ticket_id = normalize_ticket_number(ticket_id)
        self.SMARTSHEET_ACCESS_TOKEN = os.getenv("SMARTSHEET_ACCESS_TOKEN")
        self.sheet_id = 8892937224015748
        self.smart = smartsheet.Smartsheet(self.SMARTSHEET_ACCESS_TOKEN)
        self.sheet = smartsheet_api_call_with_retry(self.smart.Sheets.get_sheet, self.sheet_id)
        self.column_map = {column.title: column.id for column in self.sheet.columns}
        self.data = self.get_ticket_info()

    def get_ticket_info(self):
        row = self.find_ticket_row()
        if not row:
            return {}

        row_data = {}
        for cell in row.cells:
            column_name = next((name for name, id in self.column_map.items() if id == cell.column_id), None)
            if column_name and column_name not in ["Created By", "Created"]:
                cell_value = self.process_cell_value(cell.value, column_name)
                if cell_value is not None:
                    row_data[column_name] = cell_value

        return row_data

    def process_cell_value(self, value, column_name):
        if isinstance(value, float) and value.is_integer():
            processed_value = int(value)
        else:
            processed_value = value

        # Directly return true, skip adding if false
        if isinstance(processed_value, bool):
            return processed_value if processed_value else None

        if isinstance(processed_value, str):
            clean_value = re.sub(r'[\n\r]', '', processed_value).strip()
        else:
            clean_value = processed_value

        if column_name == "Serial Number(s)" and isinstance(clean_value, str):
            return self.parse_serial_numbers(clean_value)

        return clean_value

    @staticmethod
    def parse_serial_numbers(value):
        # Pattern to identify item names and serial numbers
        pattern = r'\[\s*(.*?)\s*\]\s*([\s\S]*?)(?=\[\s*|$)'
        items = {}

        for match in re.finditer(pattern, value):
            item_name = match.group(1).strip()
            serials_raw = match.group(2).strip()
            serials = [sn.strip() for sn in serials_raw.split() if sn.strip()]

            if item_name and serials:
                items[item_name] = serials

        return items if items else value

    def find_ticket_row(self):
        equipment_ticket_column_id = self.column_map.get("Equipment Ticket")
        if not equipment_ticket_column_id:
            print("Equipment Ticket column not found")
            return None

        for row in self.sheet.rows:
            for cell in row.cells:
                if cell.column_id == equipment_ticket_column_id:
                    cell_value = str(cell.value).strip()
                    normalized_cell_value = normalize_ticket_number(cell_value)
                    if normalized_cell_value == self.ticket_id:
                        return row

        return None

    def __str__(self):
        return json.dumps(self.data, indent=2)


class GetCWInfo:
    def __init__(self, ticket_id):
        self.CW_BASE_URL = os.getenv("CW_BASE_URL")
        self.CW_COMPANY_ID = os.getenv("CW_COMPANY_ID_PROD")
        self.CW_PUBLIC_KEY = os.getenv('CW_PUBLIC_KEY')
        self.CW_PRIVATE_KEY = os.getenv('CW_PRIVATE_KEY')
        self.ticket_id = normalize_ticket_number(ticket_id)
        self.headers = {"clientid": os.getenv('CW_CLIENT_ID')}
        self.ticket_data = self.get_ticket_by_id()
        self.data = self.get_var()

    @staticmethod
    def process_access_times(custom_fields: List[Dict[str, Any]]) -> Dict[str, str]:
        access_times: Dict[str, Dict[str, str]] = {}
        day_access: Dict[str, str] = {}

        for field in custom_fields:
            value = field.get("value")
            field_name = field.get("caption", "Unknown")

            if "Access Start | " in field_name or "Access End | " in field_name:
                day = field_name.split(" | ")[-1]
                if day not in access_times:
                    access_times[day] = {"start": "00:00", "end": "00:00"}

                if "Start" in field_name:
                    access_times[day]["start"] = value
                elif "End" in field_name:
                    access_times[day]["end"] = value
            else:
                day = field_name.split(" | ")[-1]
                if day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]:
                    day_access[day] = value

        final_access_times: Dict[str, str] = {}
        for day, times in access_times.items():
            status = day_access.get(day, "Yes").lower()
            start = times["start"]
            end = times["end"]
            if status == "no":
                final_access_times[day] = "No access"
            else:
                final_access_times[day] = f"{start}-{end}" if start != "00:00" and end != "00:00" else "No access"

        return final_access_times

    def get_var(self):
        if not self.ticket_data:
            return {}

        result = {
            "Board": self.get_it("board", "name"),
            "Summary": self.get_it("summary"),
            "Type": self.get_it("type", "name"),
            "Sub-Type": self.get_it("subType", "name"),
            "Status": self.get_it("status", "name"),
            "Customer": self.get_it("company", "name"),
            "Location": f"{self.get_it('city')}, {self.get_it('stateIdentifier')}",
            "Entered by": self.get_it("_info", "enteredBy"),
            "Date entered": self.get_it("_info", "dateEntered"),
            "Products": self.get_ticket_products()
        }

        custom_fields = self.get_it("customFields")
        if custom_fields and isinstance(custom_fields, list):
            result["Access"] = self.process_access_times(custom_fields)
            for field in custom_fields:
                field_name = field.get("caption", "Unknown").strip()
                field_value = field.get("value")
                if field_name and field_value and "Access" not in field_name and all(day not in field_name for day in
                                                                                     ["Monday", "Tuesday", "Wednesday",
                                                                                      "Thursday", "Friday", "Saturday",
                                                                                      "Sunday"]):
                    result[field_name] = field_value

        return {k: v for k, v in result.items() if v}

    def get_it(self, *path):
        data = self.ticket_data
        try:
            for key in path:
                data = data[key]
            return data.strip() if isinstance(data, str) else data
        except (KeyError, TypeError):
            return None

    def get_ticket_by_id(self):
        url = f"{self.CW_BASE_URL}/service/tickets/{self.ticket_id}"
        response = requests.get(url, auth=(f"{self.CW_COMPANY_ID}+{self.CW_PUBLIC_KEY}", self.CW_PRIVATE_KEY),
                                headers=self.headers)

        if response.status_code == 200:
            # print(json.dumps(response.json(), indent=2))
            return response.json()
        elif response.status_code == 404:  # 404 Not Found
            return None  # Return None quietly for "not found" cases
        else:
            print(f"Error fetching ticket {self.ticket_id}: {response.status_code} {response.text}")
            return None

    def get_ticket_products(self):
        products_with_details = {}

        products_url = f"{self.CW_BASE_URL}/procurement/products?conditions=ticket/id={self.ticket_id}"
        response = requests.get(products_url, auth=(f"{self.CW_COMPANY_ID}+{self.CW_PUBLIC_KEY}", self.CW_PRIVATE_KEY),
                                headers=self.headers)
        if response.status_code != 200:
            print(f"Error fetching products for ticket {self.ticket_id}: {response.status_code}")
            return {}

        for product in response.json():
            identifier = product.get("catalogItem", {}).get("identifier")
            if identifier:
                products_with_details[identifier] = {
                    "description": product.get("description"),
                    "quantity": product.get("quantity")
                }

        return products_with_details

    def __str__(self):
        return json.dumps(self.data, indent=2)


class GetGPInfo:
    def __init__(self, ticket_id):
        self.ticket_id = normalize_ticket_number(ticket_id)
        self.db_config = {
            "host": "gp2018",
            "database": "SBM01",
            "user": os.getenv('GRT_USER'),
            "password": os.getenv("GRT_PASS"),
            "tds_version": "7.0"
        }
        self.sql_query = f"""
DECLARE @TicketNumber NVARCHAR(100) = '{self.ticket_id}';

SELECT DISTINCT
    [Equipment Ticket], 
    [Account Number],
    CASE
            WHEN ([Queue] IN ('RDY TO INVOICE', 'RDY TO INV') OR [Queue] LIKE 'Q%')
THEN 'RDY TO INVOICE'
            ELSE [Queue]
    END AS [Queue],
    [Customer Name],
    [Project Name],
    [Item Number], 
    [Item Description],
    STR(
        CASE 
            WHEN FLOOR([Quantity]) = [Quantity] THEN CAST([Quantity] AS INT)
            ELSE [Quantity]
        END
    ) AS [Quantity],
    [Serial Number],
    CAST([Internal Notes] AS VARCHAR(MAX)) AS [Internal Notes],  
    [Requested Ship Date],
    [City],
    [State],
    [Tracking_Number],
    [SO Creator]
FROM (    
    SELECT
        sop30300.SOPNUMBE AS 'Equipment Ticket', 
        sop30200.CSTPONBR AS 'Account Number',
        sop30200.BACHNUMB 'Queue',
        sop30200.CUSTNAME AS 'Customer Name',
        spv3SalesDocument.xProject_Name AS 'Project Name',
        sop30300.ITEMNMBR AS 'Item Number', 
        sop30300.ITEMDESC AS 'Item Description',
        sop30300.QUANTITY AS 'Quantity',
        sop10201.SERLTNUM AS 'Serial Number',
        spv3SalesDocument.Notes AS 'Internal Notes',  
        sop30300.ReqShipDate AS 'Requested Ship Date',
        sop30300.CITY AS 'City',
        sop30300.STATE AS 'State',
        sop10107.Tracking_Number,
        sop30200.USER2ENT AS 'SO Creator'
    FROM sop30300
    FULL JOIN sop30200 ON sop30200.SOPNUMBE = sop30300.SOPNUMBE
    FULL JOIN sop10107 ON sop10107.SOPNUMBE = sop30300.SOPNUMBE
    FULL JOIN spv3SalesDocument ON spv3SalesDocument.Sales_Doc_Num = sop30300.SOPNUMBE
    FULL JOIN sop10201 ON sop10201.ITEMNMBR = sop30300.ITEMNMBR AND sop10201.SOPNUMBE = sop30300.SOPNUMBE
    FULL JOIN sop10200 ON sop30300.SOPNUMBE = sop10200.SOPNUMBE

    UNION ALL

    SELECT
        sop10100.SOPNUMBE AS 'Equipment Ticket',
        sop10100.CSTPONBR AS 'Account Number',
        sop10100.BACHNUMB AS 'Queue',
        sop10100.CUSTNAME AS 'Customer Name',
        spv3SalesDocument.xProject_Name AS 'Project Name',
        sop10200.ITEMNMBR AS 'Item Number', 
        sop10200.ITEMDESC AS 'Item Description',
        sop10200.QUANTITY AS 'Quantity',
        sop10201.SERLTNUM AS 'Serial Number',
        spv3SalesDocument.Notes AS 'Internal Notes',
        sop10100.ReqShipDate AS 'Requested Ship Date',
        sop10100.CITY AS 'City',
        sop10100.STATE AS 'State',
        sop10107.Tracking_Number,
        sop10100.USER2ENT AS 'SO Creator'
    FROM sop10100
    FULL JOIN sop10200 ON sop10200.SOPNUMBE = sop10100.SOPNUMBE
    FULL JOIN spv3SalesDocument ON spv3SalesDocument.Sales_Doc_Num = sop10100.SOPNUMBE
    FULL JOIN sop10201 ON sop10201.SOPNUMBE = sop10100.SOPNUMBE AND sop10201.ITEMNMBR = sop10200.ITEMNMBR
    FULL JOIN sop10107 ON sop10107.SOPNUMBE = sop10100.SOPNUMBE
) AS MASTER_GP_QUERY
WHERE [Equipment Ticket] IN ('CW' + @TicketNumber + '-1', @TicketNumber);
"""
        self.data = self.query_gp()

    def query_gp(self):
        connection = None
        item_key = None
        try:
            connection = pymssql.connect(**self.db_config)
            cursor = connection.cursor(as_dict=True)
            cursor.execute(self.sql_query)
            rows = cursor.fetchall()

            if not rows:
                return {}

            result_data = {}
            items = {}

            for row in rows:
                for k, v in row.items():
                    if v is None:  # Skip None values outright
                        continue

                    if isinstance(v, str):
                        v = v.strip()  # Strip string values
                        if not v:  # If the string is empty after stripping, skip it
                            continue

                    # Convert datetime objects to string
                    if isinstance(v, datetime.datetime):
                        v = v.isoformat()

                    # Processing specific fields
                    if k == 'Internal Notes':
                        result_data[k] = [line for line in re.split(r'\r\n|\r|\n', v) if line]
                    elif k in ['Item Number', 'Item Description', 'Serial Number', 'Quantity']:
                        # Handling item details
                        if k == 'Item Number' and v:
                            item_key = v
                            items[item_key] = items.get(item_key, {'Serial Numbers': []})
                        elif k == 'Item Description' and item_key in items:
                            items[item_key]['Item Description'] = v
                        elif k == 'Quantity' and item_key in items:
                            items[item_key]['Quantity'] = v
                        elif k == 'Serial Number' and item_key in items:
                            items[item_key]['Serial Numbers'].append(v)
                    else:
                        result_data[k] = v

            if items:
                result_data['Items'] = items

            return result_data

        except Exception as e:
            logger.error(f"Database connection error: {str(e)}")
            return {"error": str(e)}
        finally:
            if connection:
                connection.close()

    def __str__(self):
        return json.dumps(self.data, indent=2) or 'No data available'


class GetCSInfo:
    def __init__(self, ticket_id):
        self.ticket_id = normalize_ticket_number(ticket_id)
        self.db_config = {
            "host": "ods",
            "database": "ODS",
            "user": os.getenv('GRT_USER'),
            "password": os.getenv("GRT_PASS"),
            "tds_version": "7.0"
        }
        self.sql_query = f"""
select distinct * from (
    select distinct
        TICKETS_CORE_VIEW.TICKET_ID as 'Ticket',
        TICKETS_CORE_VIEW.MACNUM as 'Child Account',
        TICKETS_CORE_VIEW.TICKET_TYPE as 'Ticket Type',
        TICKETS_CORE_VIEW.TICKET_SUB_TYPE as 'Ticket Sub-type',
        TICKETS_CORE_VIEW.STATUS as 'Status',
        Assignee.NAME as 'Assigned To',
        TICKETS_CORE_VIEW.LOGGED_DT as 'Creation Date',
        TICKETS_CORE_VIEW.SUBJECT as 'Details',
        Creator.NAME as 'Ticket Creator'
    from Tickets.TICKETS_CORE_VIEW
    full join People.EMPLOYEES Creator on Creator.EMPLOYEE_ID = TICKETS_CORE_VIEW.LOGGED_BY
    full join People.EMPLOYEES Assignee on Assignee.EMPLOYEE_ID = TICKETS_CORE_VIEW.ASSIGNED_TO

    UNION

    select distinct
        Tickets.CAN_TICKETS_CORE.TICKET_ID as 'Ticket',
        Tickets.CAN_TICKETS_CORE.MACNUM as 'Child Account',
        cast(Tickets.CAN_TICKETS_CORE.TICKET_TYPES_ID as nvarchar) as 'Ticket Type',
        cast(Tickets.CAN_TICKETS_CORE.TICKET_SUB_TYPES_ID as nvarchar) as 'Ticket Sub-type',
        cast(Tickets.CAN_TICKETS_CORE.TICKET_STATUS_ID as nvarchar) as 'Status',
        Assignee2.NAME as 'Assigned To',
        Tickets.CAN_TICKETS_CORE.LOGGED_DT as 'Creation Date',
        Tickets.CAN_TICKETS_CORE.SUBJECT as 'Details',
        Creator2.NAME as 'Ticket Creator'
    from Tickets.CAN_TICKETS_CORE
    full join People.EMPLOYEES Creator2 on Creator2.EMPLOYEE_ID = Tickets.CAN_TICKETS_CORE.LOGGED_BY
    full join People.EMPLOYEES Assignee2 on Assignee2.EMPLOYEE_ID = Tickets.CAN_TICKETS_CORE.ASSIGNED_TO
) as MASTER_CS_QUERY
where [Ticket] = '{self.ticket_id}';
"""
        self.data = self.query_cs()

    @staticmethod
    def clean_text(text):
        # Remove unwanted characters and whitespace from the text
        text = re.sub(r'[\u2022\t\r\n]+', ' ', text)  # Replace bullets, tabs, and newlines with spaces
        text = re.sub(r'\s+', ' ', text)  # Replace multiple spaces with a single space
        return text.strip()  # Remove leading and trailing whitespace

    def parse_details(self, details):
        # Split the details into lines first
        lines = details.split('\n')

        # Then clean each line and build the cleaned details list
        cleaned_lines = [self.clean_text(line) for line in lines if line.strip()]

        return cleaned_lines

    def query_cs(self):
        connection = None
        try:
            connection = pymssql.connect(**self.db_config)
            cursor = connection.cursor(as_dict=True)
            cursor.execute(self.sql_query)
            rows = cursor.fetchall()

            result_data = {}

            for row in rows:
                processed_row = {}
                for key, value in row.items():
                    if value is None:
                        processed_row[key] = None
                    elif isinstance(value, decimal.Decimal) and value % 1 == 0:
                        processed_row[key] = int(value)
                    elif isinstance(value, float) and value.is_integer():
                        processed_row[key] = int(value)
                    elif isinstance(value, datetime.datetime):
                        processed_row[key] = value.isoformat()
                    elif isinstance(value, str) and key == 'Details':
                        processed_row[key] = self.parse_details(value)
                    else:
                        processed_row[key] = value.strip() if isinstance(value, str) else value

                if processed_row:
                    result_data.update(processed_row)

            return result_data

        except Exception as e:
            print(f"Database connection error: {str(e)}")
            return {"error": str(e)}

        finally:
            if connection:
                connection.close()

    def __str__(self):
        return json.dumps(self.data, indent=2) or 'No data available'


class GetWOMInfo:
    def __init__(self, ticket_id):
        self.ticket_id = normalize_ticket_number(ticket_id)
        self.db_config = {
            "host": "ods",
            "database": "ODS",
            "user": os.getenv('GRT_USER'),
            "password": os.getenv("GRT_PASS"),
            "tds_version": "7.0"
        }
        self.sql_query = f"""
select distinct * from (
select
WOM.customerinformation$provisioningworkorder.provisioningwonumber as 'Ticket',
WOM.customerinformation$provisioningworkorder.provwotype as 'Ticket Type',
WOM.customerinformation$provisioningworkorderstatus.[name] as 'Status',
WOM.customerinformation$provisioningworkorderdetails.createddate as 'Creation Date',
WOM.usermanagement$account.fullname as 'Created by'


from 
WOM.customerinformation$provisioningworkorder

left join WOM.customerinformation$provisioningworkorderdetails_provisioningworkorder on WOM.customerinformation$provisioningworkorderdetails_provisioningworkorder.customerinformation$provisioningworkorderid = WOM.customerinformation$provisioningworkorder.id
left join WOM.customerinformation$provisioningworkorderdetails on WOM.customerinformation$provisioningworkorderdetails.id = WOM.customerinformation$provisioningworkorderdetails_provisioningworkorder.customerinformation$provisioningworkorderdetailsid
left join WOM.usermanagement$account on WOM.usermanagement$account.id = WOM.customerinformation$provisioningworkorder.system$owner
left join WOM.customerinformation$provisioningworkorder_provisioningworkorderstatus on WOM.customerinformation$provisioningworkorder_provisioningworkorderstatus.customerinformation$provisioningworkorderid = WOM.customerinformation$provisioningworkorder.id
left join WOM.customerinformation$provisioningworkorderstatus on WOM.customerinformation$provisioningworkorderstatus.id = WOM.customerinformation$provisioningworkorder_provisioningworkorderstatus.customerinformation$provisioningworkorderstatusid	



) as MASTER_WOM_QUERY
where [Ticket] = '{self.ticket_id}'
"""
        self.data = self.query_wom()

    def parse_details(self, details):
        lines = details.split('\u2022')[1:]  # Skip the first element if details start with a bullet
        parsed_details = {}

        for line in lines:
            parts = line.split(':', 1)  # Split only at the first colon
            if len(parts) == 2:
                key, value = [part.strip() for part in parts]
                if key:
                    if value:
                        if 'o\tPart:' in value:
                            items = self.parse_sub_items(value)
                            # Only add the key if items is not None
                            if items is not None:
                                parsed_details[key] = items
                        else:
                            # Only add the key if value is not empty
                            parsed_details[key] = value.replace('\r\n', ' ').replace('\n', ' ')
            else:
                # Handle lines without a colon as a separate case, if needed
                line_content = line.strip()
                if line_content:
                    # This could be a standalone line or title; handle accordingly
                    parsed_details[line_content] = None

        # Remove keys with None values
        parsed_details = {k: v for k, v in parsed_details.items() if v is not None}

        return parsed_details

    @staticmethod
    def parse_sub_items(items_str):
        # Split the items string on the specified pattern
        items = []
        for item in items_str.split('o\t'):
            cleaned_item = item.strip().replace('\r\n', ' ').replace('\n', ' ')
            # Only add the item to the list if it's not empty
            if cleaned_item:
                items.append(cleaned_item)

        return items or None  # Return None if the list is empty

    def query_wom(self):
        connection = None
        try:
            connection = pymssql.connect(**self.db_config)
            cursor = connection.cursor(as_dict=True)
            cursor.execute(self.sql_query)
            rows = cursor.fetchall()

            result_data = {}
            if not rows:
                return result_data  # Return empty dict if no rows found

            for row in rows:
                for key, value in row.items():
                    if isinstance(value, decimal.Decimal) and value % 1 == 0:
                        value = int(value)
                    elif isinstance(value, float) and value.is_integer():
                        value = int(value)
                    elif isinstance(value, datetime.datetime):
                        value = value.isoformat()
                    elif isinstance(value, str):
                        value = value.strip()

                    if value:  # Add to result if value is meaningful (not None, not empty string, etc.)
                        result_data[key] = value

            return result_data
        except Exception as e:
            print(f"Database connection error: {str(e)}")
            return {"error": str(e)}
        finally:
            if connection:
                connection.close()

    def __str__(self):
        return json.dumps(self.data, indent=2) or 'No data available'


class TicketAggregator:
    def __init__(self, ticket_id):
        self.ticket_id = ticket_id
        self.ss_info = GetSSInfo(ticket_id)
        self.gp_info = GetGPInfo(ticket_id)

    def get_data_from_source(self, source_class):
        source = source_class(self.ticket_id)
        return source.data

    def aggregate_data(self):
        aggregated_data = {
            "Smartsheet": self.ss_info.data,
            "Salespad/GP": self.gp_info.data,
        }

        # ConnectWise data
        cw_data = self.get_data_from_source(GetCWInfo)
        if cw_data:
            aggregated_data["ConnectWise"] = cw_data
        else:
            # WOM data
            wom_data = self.get_data_from_source(GetWOMInfo)
            if wom_data:
                aggregated_data["WOM"] = wom_data
            else:
                # Cornerstone data
                cs_data = self.get_data_from_source(GetCSInfo)
                if cs_data:
                    aggregated_data["Cornerstone"] = cs_data

        return {k: v for k, v in aggregated_data.items() if v}

    def __str__(self):
        aggregated_data = self.aggregate_data()
        return json.dumps(aggregated_data, indent=2)


if __name__ == '__main__':
    # tkt = "164944290"
    # print(TicketAggregator(tkt))
    # # print(GetCSInfo(tkt))
    #
    # tkt2 = "163398249"
    # print(TicketAggregator(tkt2))
    # print(GetCSInfo(tkt2))

    tkt = """
    3813479   
    """.strip()
    # print(GetGPInfo(tkt))
    print(TicketAggregator(tkt))
