#and, not, or

temp = int(input("What is the temperature outside?: "))
if temp > 0 and temp < 30:
    print("The weather is fine \nGo outside!")
elif temp <= 0:
    print("Sounds like cold!\nStay indside!")