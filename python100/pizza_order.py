# Small Pizza: $15
#
# Medium Pizza: $20
#
# Large Pizza: $25
#
# Pepperoni for Small Pizza: +$2
#
# Pepperoni for Medium or Large Pizza: +$3
#
# Extra cheese for any size pizza: + $1

pizza = input("What pizza size do you want? S, M or L \n")
extra_pepperoni = input("Do you want any extra pepperoni on it? Y or N \n")
extra_cheese = input("Do you want any extra cheese on it? Y or N \n")
bill = 0


if pizza == "S":
    bill += 15
    if extra_pepperoni == "Y":
        bill += 2
    if extra_cheese == "Y":
        bill += 1
        print(f"Your total bill is $ {bill}")

    if extra_pepperoni == "N":
        bill = bill
    if extra_cheese == "N":
        bill = bill
        print(f"Your totall bill is $ {bill}")

if pizza == "M":
    bill += 20
    if extra_pepperoni == "Y":
        bill += 3
    if extra_cheese == "Y":
        bill += 1
        print(f"Your total bill is $ {bill}")
    if extra_pepperoni == "N":
        bill = bill
    if extra_cheese == "N":
        bill = bill
        print(f"Your totall bill is $ {bill}")



if pizza == "L":
    bill += 25
    if extra_pepperoni == "Y":
        bill += 3
    if extra_cheese == "Y":
        bill += 1
        print(f"Your total bill is $ {bill}")
    if extra_pepperoni == "N":
        bill = bill
    if extra_cheese == "N":
        bill = bill
        print(f"Your totall bill is $ {bill}")