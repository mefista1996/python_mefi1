import requests
TOKEN = '5353499656:AAFCg4YCSKo_6jCO9q6FFGTl0OYO1Ph2XLE'
URL = 'https://api.telegram.org/bot{TOKEN}/{method}'
updates = 'getUpdates'
url = URL.format(TOKEN=TOKEN, method=updates)
response = requests.get(url)
print (dir(response))
print(response.text)
print(response.content)