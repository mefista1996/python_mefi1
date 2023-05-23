#dict = changable, unordered collection of unique key = value pairs

capitals = {'France': 'Paris',
            'Italy': 'Roma',
            'Ukraine':'Kyiv'}

#print(capitals['France'])

print(capitals.get('Italy'))

#methods - dict.get(value), dict.keys(), dict.values(),
# dict.items(), dict.update({}),dict.pop(key), dict.clear()

for key, value in capitals.items():
    print(key, value)