import datetime



def convert_datetime(value):
            if isinstance(value, datetime.datetime):
                return value.strftime('%Y-%m-%d %H:%M')
            if isinstance(value, datetime.date):
                return value.strftime('%Y-%m-%d')
            return value