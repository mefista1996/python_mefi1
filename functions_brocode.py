#is a block of code which is executed only per call

def hello(name, time):
    print("Nice to see you learning %s.\nIt only takes %d minutes a day to be learning" % (name, time))

hello("Victoria", 30)


#return statement = a callback -= function returns the results when called


def multiply(num1,num2):
    return num1*num2
x = multiply(6,8)

print(x)
